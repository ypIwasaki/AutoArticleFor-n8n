from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import weekly_metrics as metrics
import article_review_facts as shared
import read_ai_inputs as reader
from article_feedback_snapshot import build_snapshot


class WeeklyMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in shared.POLICY_FILES:
            self.text(name, "test policy")
        self.json("config/article-classification-taxonomy.json",
                  json.loads((ROOT / "config/article-classification-taxonomy.json").read_text()))
        self.json("config/keyword-aliases.json",
                  json.loads((ROOT / "config/keyword-aliases.json").read_text()))
        self.end = "2026-09-10"

    def text(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def json(self, name, data):
        return self.text(name, json.dumps(data, ensure_ascii=False))

    def jsonl(self, name, data):
        return self.text(name, "\n".join(json.dumps(row, ensure_ascii=False) for row in data) + "\n")

    def archive(self, label, urls, source="媒体A", capture=True):
        run = {"recordType": "run", "runDate": label, "articleCount": len(urls),
               "capturedArticleCount": len(urls), "keywords": ["にじさんじ"]}
        articles = [{"recordType": "article", "runDate": label, "articleIndex": i + 1,
                     "article": {"url": url, "title": "にじさんじがライブを発表", "excerpt": "出演情報",
                                 "publishedAt": "2026-09-07T00:00:00Z", "source": source}}
                    for i, url in enumerate(urls)]
        captures = [{"originalUrl": url, "resolvedUrl": url, "contentStatus": "verified",
                     "contentType": "article", "contentText": "にじさんじがライブを開催する。出演者を発表した。",
                     "sourceDomain": metrics.host(url)} for url in dict.fromkeys(urls)]
        self.jsonl(f"content/structured-records/{label}.jsonl", [run, *articles])
        if capture:
            self.jsonl(f"content/article-body-captures/{label}.jsonl", captures)
        return articles, captures

    def feedback(self, rows=(), label=None, complete=True):
        label = label or self.end
        rows = [{"articleUrl": row[0], "decision": row[1],
                 "reasonCode": row[2] if len(row) > 2 else "approved",
                 "sourceDomain": row[3] if len(row) > 3 else "",
                 "publisherLabel": row[4] if len(row) > 4 else "",
                 "reviewedAt": label + "T00:00:00Z"} for row in rows]
        return self.json(f"content/article-feedback-instructions/{label}.json",
                         {"schemaVersion": 1, "snapshotDate": label, "generatedAt": label + "T12:00:00+09:00",
                          "complete": complete, "feedback": rows})

    def classification(self, url, category="event", label=None, **extra):
        label = label or self.end
        return {"article_url": url, "article_type": "news_article", "primary_category": category,
                "secondary_categories_json": [], "relevance": "in_scope", "confidence": 0.9,
                "evidence_text": "本文でイベント開催を確認", "classification_method": "ai_review",
                "classified_at": label + "T00:00:00Z", **extra}

    def review(self, label, article, capture, status="ready", basis="body", names=()):
        record = {"reviewVersion": 1, "url": article["url"], "inputHash": shared.input_hash(article, capture),
                  "policyHash": shared.policy_hash(self.root), "reviewedBy": "test",
                  "basis": basis, "taskStatus": {task: status for task in shared.TASKS},
                  "facts": [{"id": "f1", "text": "ライブ開催", "evidenceIds": ["e1"]}],
                  "entities": [{"name": name, "kind": "organization", "factIds": ["f1"]} for name in names],
                  "evidence": [{"id": "e1", "field": "contentText", "start": 0, "end": 3, "quote": capture["contentText"][:3]}],
                  "unresolved": [] if status == "ready" else ["全工程: 未確認"],
                  "sourceDate": label, "reviewedAt": label + "T00:00:00Z"}
        shared.validate_record(record, article, capture, shared.policy_hash(self.root))
        self.jsonl(f"{shared.DIRECTORY}/{label}.jsonl", [record])
        return record

    def build(self, through=None, as_of=None):
        return metrics.build_metrics(self.root, through or self.end, as_of)

    def test_counts_missing_day_zero_day_and_latest_url_record(self):
        self.archive("2026-09-07", ["https://a.test/1", "https://a.test/2"])
        self.archive("2026-09-09", [])
        self.archive(self.end, ["https://a.test/1", "https://a.test/3"])
        self.feedback()
        result = self.build()
        self.assertEqual(result["counts"]["archivedRecords"], 4)
        self.assertEqual(result["counts"]["uniqueArticles"], 3)
        self.assertEqual(result["counts"]["duplicateRecords"], 1)
        self.assertEqual(result["coverage"]["missingDates"], ["2026-09-08"])
        self.assertEqual(result["daily"][2]["archived"], 0)
        self.assertEqual(result["articleDecisions"][0]["runDate"], self.end)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result, self.build())

    def test_through_and_evaluation_as_of_are_separate(self):
        url = "https://a.test/1"
        self.archive("2026-09-07", [url])
        self.archive(self.end, ["https://future.test/1"])
        self.feedback([(url, "rejected", "irrelevant")])
        self.assertEqual(self.build("2026-09-07")["counts"]["excludedArticles"], 0)
        result = self.build("2026-09-07", self.end)
        self.assertEqual(result["counts"]["archivedRecords"], 1)
        self.assertEqual(result["counts"]["excludedArticles"], 1)
        with self.assertRaises(ValueError): self.build(self.end, "2026-09-07")

    def test_exact_url_and_source_rejections_do_not_expand_other_reasons(self):
        urls = ["https://a.test/1", "https://a.test/2", "https://b.test/1", "https://b.test/2", "https://sub.b.test/1"]
        self.archive(self.end, urls)
        self.feedback([(urls[0], "rejected", "outdated"),
                       (urls[2], "rejected", "suspicious_source", "b.test")])
        result = self.build()
        self.assertEqual(result["counts"]["excludedArticles"], 3)
        self.assertEqual(result["counts"]["eligibleArticles"], 2)
        self.assertEqual(result["exclusionReasons"]["source:suspicious_source"], 2)
        self.assertEqual(sum(result["bySource"][k]["count"] for k in result["bySource"]), 2)

    def test_source_label_rejection_and_approved_url_conflict(self):
        urls = ["https://a.test/1", "https://a.test/2"]
        self.archive(self.end, urls, source="媒体A")
        self.feedback([(urls[0], "rejected", "suspicious_source", "", "媒体A"), (urls[1], "approved")])
        self.assertEqual(self.build()["counts"]["eligibleArticles"], 0)

    def test_latest_complete_snapshot_can_revoke_prior_rejection(self):
        url = "https://a.test/1"
        self.archive(self.end, [url])
        self.feedback([(url, "rejected", "irrelevant")], label="2026-09-07")
        self.feedback([(url, "approved")])
        self.assertEqual(self.build()["counts"]["excludedArticles"], 0)

    def test_missing_incomplete_and_stale_feedback_is_provisional(self):
        self.archive(self.end, ["https://a.test/1"])
        self.text("content/article-feedback-instructions/README.md", "Not a dated snapshot")
        self.assertEqual(self.build()["feedback"]["status"], "missing")
        self.feedback(complete=False)
        self.assertEqual(self.build()["feedback"]["status"], "incomplete")
        self.feedback(label="2026-09-09")
        (self.root / f"content/article-feedback-instructions/{self.end}.json").unlink()
        self.text(f"content/article-feedback-instructions/{self.end}.md", "newer guidance")
        self.assertEqual(self.build()["feedback"]["status"], "stale")
        with self.assertRaisesRegex(ValueError, "Full feedback"):
            metrics.main(self.root, ["--through", self.end, "--require-feedback"])
        self.assertFalse((self.root / "content/analysis").exists())

    def test_same_day_guidance_hash_mismatch_is_not_applied(self):
        self.archive(self.end, ["https://a.test/1"])
        self.feedback()
        self.text(f"content/article-feedback-instructions/{self.end}.md", "changed")
        self.assertEqual(self.build()["feedback"]["status"], "stale")

    def test_classification_denominators_invalid_and_unbound_rows(self):
        urls = ["https://a.test/1", "https://a.test/2", "https://a.test/3"]
        self.archive(self.end, urls)
        self.feedback()
        self.json(f"content/article-classification-proposals/{self.end}.json",
                  {"classifications": [self.classification(urls[0]), self.classification(urls[1], category="other"),
                                       self.classification(urls[2], confidence=2)]})
        result = self.build()
        self.assertEqual(result["counts"]["classifiedArticles"], 2)
        self.assertEqual(result["counts"]["invalidClassificationArticles"], 1)
        self.assertEqual(result["byCategory"]["event"], {"count": 1, "denominator": 2, "percent": 50.0})
        self.assertEqual(result["classificationCoverage"]["percent"], 66.67)
        self.assertEqual(result["counts"]["unboundClassifications"], 2)
        self.assertEqual(result["counts"]["bodyReviewReadyArticles"], 0)

    def test_future_and_source_mismatched_classifications_not_counted(self):
        url = "https://a.test/1"
        self.archive("2026-09-07", [url])
        self.json("content/article-classification-proposals/2026-09-11.json",
                  {"classifications": [self.classification(url, label="2026-09-11")]})
        self.assertEqual(self.build()["counts"]["classifiedArticles"], 0)
        self.json(f"content/article-classification-proposals/{self.end}.json",
                  {"classifications": [self.classification(url, inputHash="wrong")]})
        self.assertEqual(self.build()["counts"]["invalidClassificationArticles"], 1)

    def test_capture_success_is_not_shared_review_and_entities_are_url_unique(self):
        urls = ["https://a.test/1", "https://a.test/1"]
        articles, captures = self.archive(self.end, urls)
        self.feedback()
        self.assertEqual(self.build()["counts"]["bodyReviewReadyArticles"], 0)
        self.review(self.end, articles[0]["article"], captures[0], names=["NIJISANJI", "にじさんじ"])
        result = self.build()
        self.assertEqual(result["counts"]["bodyReviewReadyArticles"], 1)
        self.assertEqual(result["entities"], [{"kind": "organization", "name": "にじさんじ",
                                             "articles": 1, "representativeDates": [self.end]}])

    def test_old_capture_not_mixed_with_latest_missing_capture(self):
        url = "https://a.test/1"
        articles, captures = self.archive("2026-09-07", [url])
        self.review("2026-09-07", articles[0]["article"], captures[0])
        self.archive(self.end, [url], capture=False)
        self.assertEqual(self.build()["captureStatuses"], {"not_captured": 1})
        self.assertEqual(self.build()["counts"]["bodyReviewReadyArticles"], 0)

    def test_zero_and_missing_week_and_year_boundary(self):
        result = self.build("2027-01-03")
        self.assertEqual(result["week"], "2026-W53")
        self.assertEqual(result["weekStart"], "2026-12-28")
        self.assertEqual(len(result["coverage"]["missingDates"]), 7)
        self.assertIsNone(result["classificationCoverage"]["percent"])
        self.assertIsNone(result["counts"]["videoMetadataSummaries"])
        result = self.build("2027-01-04")
        self.assertEqual(result["week"], "2027-W01")
        self.assertEqual(len(result["daily"]), 1)

    def test_candidates_cutoff_and_malformed_rows(self):
        header = "| Candidate | Category | Confidence | Add | Reason | Evidence |\n| --- | --- | --- | --- | --- | --- |\n"
        self.text("content/ai-keyword-candidates/2026-09-07.md", header + "| A | company | 0.9 | yes | 理由 | 根拠 |\n")
        self.text("content/ai-keyword-candidates/2026-09-11.md", "| invalid future |")
        self.assertEqual(self.build()["keywordCandidates"][0]["add_count"], 1)
        self.text("content/ai-keyword-candidates/2026-09-09.md", "| bad |")
        with self.assertRaisesRegex(ValueError, "Incomplete keyword"): self.build()

    def test_output_consistency_staleness_and_report_prose_preservation(self):
        self.archive(self.end, ["https://a.test/1"])
        result = self.build()
        paths = metrics.write_outputs(self.root, result)
        metrics.check_outputs(self.root, result)
        report = self.text("content/weekly-reports/2026-09-07.md", "# Report\n\n私の考察は維持する。\n")
        metrics.sync_report(report, result)
        metrics.sync_report(report, result)
        self.assertEqual(report.read_text().count(metrics.START), 1)
        self.assertIn("私の考察は維持する。", report.read_text())
        metrics.check_report(report, result)
        report.write_text(report.read_text().replace("archivedRecords | 1", "archivedRecords | 99"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "numeric block"): metrics.check_report(report, result)
        loaded = json.loads(paths[0].read_text())
        loaded["counts"]["eligibleArticles"] = 99
        paths[0].write_text(json.dumps(loaded), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale or altered"): metrics.check_outputs(self.root, result)

    def test_input_change_invalidates_snapshot_even_if_counts_same(self):
        self.archive(self.end, ["https://a.test/1"])
        first = self.build()
        metrics.write_outputs(self.root, first)
        capture = self.root / f"content/article-body-captures/{self.end}.jsonl"
        capture.write_text(capture.read_text().replace("出演者", "登壇者"), encoding="utf-8")
        second = self.build()
        self.assertEqual(first["counts"], second["counts"])
        self.assertNotEqual(first["snapshotId"], second["snapshotId"])
        with self.assertRaises(ValueError): metrics.read_weekly_input(self.root, self.end)

    def test_reader_default_is_compact_and_raw_pages_are_explicit(self):
        self.archive(self.end, ["https://a.test/1"])
        with self.assertRaisesRegex(ValueError, "Generate current metrics"): metrics.read_weekly_input(self.root, self.end)
        metrics.write_outputs(self.root, self.build())
        output = io.StringIO()
        with patch.object(reader, "ROOT", self.root), contextlib.redirect_stdout(output):
            self.assertEqual(reader.main(["--run-date", self.end, "--task", "weekly-report"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["articles"], [])
        self.assertNotIn("articleDecisions", payload["metrics"])
        output = io.StringIO()
        with patch.object(reader, "ROOT", self.root), contextlib.redirect_stdout(output):
            self.assertEqual(reader.main(["--run-date", self.end, "--task", "weekly-report", "--weekly-articles"]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["articles"]), 1)

    def test_cli_check_no_writes_and_arguments_validated(self):
        self.archive(self.end, [])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(metrics.main(self.root, ["--through", self.end]), 0)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(metrics.main(self.root, ["--through", self.end, "--check"]), 0)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        with self.assertRaises(ValueError): metrics.main(self.root, ["--week", "2026-W36", "--through", self.end])
        with self.assertRaises(ValueError): metrics.main(self.root, ["--week", "2026-W99"])

    def test_invalid_archive_feedback_and_duplicate_classification_fail(self):
        self.archive(self.end, ["https://a.test/1"])
        path = self.feedback([("https://a.test/1", "rejected", "bad_reason")])
        with self.assertRaisesRegex(ValueError, "rejection reason"): self.build()
        path.unlink()
        self.json(f"content/article-classification-proposals/{self.end}.json",
                  {"classifications": [self.classification("https://a.test/1")] * 2})
        with self.assertRaisesRegex(ValueError, "Duplicate classification"): self.build()

    def test_snapshot_exports_all_feedback_not_ten_examples(self):
        generated = datetime(2026, 9, 10, 12, tzinfo=metrics.JST)
        payload = {"articles": [{"article_key": str(i), "url": f"https://a.test/{i}"} for i in range(15)],
                   "article_feedback": [{"article_key": str(i), "is_rejected": True, "reason_code": "irrelevant",
                                         "reviewed_at": "2026-09-10T00:00:00Z"} for i in range(15)]}
        snapshot = build_snapshot(payload, generated, "guide")
        self.assertTrue(snapshot["complete"])
        self.assertEqual(len(snapshot["feedback"]), 15)
        self.text(f"content/article-feedback-instructions/{self.end}.md", "guide")
        self.json(f"content/article-feedback-instructions/{self.end}.json", snapshot)
        self.archive(self.end, [f"https://a.test/{i}" for i in range(15)])
        self.assertEqual(self.build()["counts"]["excludedArticles"], 15)
        payload["_article_feedback_available"] = False
        self.assertFalse(build_snapshot(payload, generated, "guide")["complete"])
        del payload["_article_feedback_available"]
        payload["article_feedback"][0]["article_key"] = "unmapped"
        self.assertFalse(build_snapshot(payload, generated, "guide")["complete"])

    def test_dashboard_writer_emits_both_formats_without_db_calls(self):
        spec = importlib.util.spec_from_file_location("weekly_feedback_dashboard_test", ROOT / "apps/talent-dashboard/server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        generated = datetime(2026, 9, 10, 12, tzinfo=metrics.JST)
        with patch.object(server, "PROJECT_ROOT", self.root), patch.object(server, "load_from_n8n", side_effect=AssertionError("DB called")):
            path = server.write_article_feedback_instruction({"articles": [], "article_feedback": []}, generated)
        self.assertTrue(path.is_file())
        snapshot = json.loads(path.with_suffix(".json").read_text())
        self.assertTrue(snapshot["complete"])
        self.assertEqual(self.build()["feedback"]["status"], "complete")

    def test_export_invalid_review_timestamp_and_unknown_source_scope(self):
        generated = datetime(2026, 9, 10, 12, tzinfo=metrics.JST)
        row = {"article_url": "https://a.test/1", "is_rejected": True,
               "reason_code": "irrelevant", "reviewed_at": "2026-09-10T00:00:00Z"}
        for value in ("", "bad", "2026-09-10T13:00:00+09:00"):
            payload = {"article_feedback": [{**row, "reviewed_at": value}]}
            self.assertFalse(build_snapshot(payload, generated, "guide")["complete"])
        row["reason_code"] = "suspicious_source"
        self.assertFalse(build_snapshot({"article_feedback": [row]}, generated, "guide")["complete"])
        self.feedback([("https://a.test/1", "rejected", "suspicious_source")])
        with self.assertRaisesRegex(ValueError, "domain or publisher"):
            self.build()


if __name__ == "__main__":
    unittest.main()
