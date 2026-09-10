import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import read_ai_inputs as reader
from capture_article_contents import progress_line

class CompactInputTests(unittest.TestCase):
    def payload(self):
        return dict(task="article-summary", runDate="2026-09-10", offset=40, nextOffset=41, articles=[dict(articleIndex=99, runDate="2026-09-10", title="題"*300, url="https://example.test/long", contentStatus="partial", failureReason="理由"*200, content=dict(text="body"*6000), reviewInput=dict(inputHash="hash"), sharedReview=dict(status="current",taskStatus="held",facts=["long evidence"]))], warnings=["missing archive"], context=dict(runs=[dict(keywords=["keyword"]*400, articleCount=627)], captureStatusCounts=dict(partial=1)))
    def test_inventory_preserves_navigation_holds_and_warns_about_omissions(self):
        source=self.payload();original=copy.deepcopy(source);result=reader.inventory_payload(source)
        self.assertEqual(source,original)
        self.assertEqual(result["nextOffset"],41)
        row=result["articles"][0]
        self.assertEqual(row["recordOffset"],40)
        self.assertEqual(row["articleIndex"],99)
        self.assertEqual(row["sharedReview"],dict(status="current",taskStatus="held"))
        self.assertTrue(row["titleTruncated"])
        self.assertTrue(row["failureReasonTruncated"])
        self.assertTrue(result["reviewEvidenceOmitted"])
        self.assertNotIn("url",row)
        self.assertNotIn("content",row)
        self.assertNotIn("reviewInput",row)
        self.assertEqual(result["warnings"],["missing archive"])
        self.assertEqual(result["context"]["runs"][0]["keywordCount"],400)
        self.assertLess(len(json.dumps(result)),len(json.dumps(source))//4)
    def test_default_detail_keeps_evidence_and_inventory_cli_is_opt_in(self):
        for extra,compact in [([],False),(["--view","inventory"],True)]:
            output=io.StringIO()
            with patch.object(reader,"build_payload",return_value=self.payload()),contextlib.redirect_stdout(output):
                self.assertEqual(reader.main(["--run-date","2026-09-10","--task","article-summary",*extra]),0)
            result=json.loads(output.getvalue())
            self.assertEqual("content" not in result["articles"][0],compact)
    def test_inventory_rejects_body_request(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reader.main(["--run-date","2026-09-10","--task","article-summary","--view","inventory","--include-body"]),2)
    def test_capture_summary_keeps_all_statuses_without_titles(self):
        result=json.loads(progress_line(3,10,[dict(status="verified",title="hidden"),dict(status="unavailable"),dict(status="partial")]))
        self.assertEqual(result["captureStatuses"],dict(verified=1,unavailable=1,partial=1))
        self.assertEqual(result["verification"],"capture_status_only")
        self.assertNotIn("hidden",str(result))
