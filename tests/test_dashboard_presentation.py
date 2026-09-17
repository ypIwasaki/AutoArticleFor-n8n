"""Dashboard presentation contracts; only weekly fixtures touch temporary files."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from talent_dashboard_presenter import build_dashboard_payload
import weekly_report_reader as weekly
import project_readers


class DashboardPresentationTests(unittest.TestCase):
    def setUp(self):
        self.generated_at = datetime(2026, 9, 17, 1, tzinfo=timezone.utc)
        self.payload = {
            "talents": [
                {"talent_id": "t1", "display_name": "Alice", "organization": "Agency",
                 "aliases_json": '["A"]', "status": "approved", "search_enabled": True},
                {"talent_id": "t2", "display_name": "Bob", "organization": "",
                 "aliases_json": [], "status": "pending", "search_enabled": False},
            ],
            "articles": [
                {"article_key": "a1", "url": "https://example.test/1", "title": "First",
                 "published_at": "2026-09-16T00:00:00Z"},
                {"article_key": "a2", "url": "https://example.test/2", "title": "Rejected",
                 "published_at": "2026-09-17T00:00:00Z"},
                {"article_key": "a3", "url": "https://example.test/3", "title": "Third",
                 "last_seen_at": "2026-09-15T00:00:00Z"},
            ],
            "article_talents": [
                {"article_key": "a1", "talent_id": "t1", "last_seen_at": "2026-09-16"},
                {"article_key": "a1", "talent_id": "t2", "last_seen_at": "2026-09-17"},
                {"article_key": "a2", "talent_id": "t1", "last_seen_at": "2026-09-18"},
            ],
            "article_feedback": [
                {"article_key": "a1", "is_rejected": False},
                {"article_key": "a2", "is_rejected": "true"},
            ],
            "article_classifications": [
                {"article_key": "a1", "article_type": "news", "primary_category": "music",
                 "relevance": "in_scope"}
            ],
        }
        self.inputs = {
            "source": "project-db",
            "source_error": None,
            "article_summaries": {
                "https://example.test/1": {"text": "Reviewed", "summary_date": "2026-09-17"},
                "https://example.test/2": {"text": "Rejected summary"},
            },
            "classification_taxonomy": {"categories": [{"id": "music"}]},
            "official_registry": {
                "generatedAt": "2026-09-17",
                "talents": [
                    {"display_name": "A", "organization": " Agency ",
                     "profile_url": "https://official.test/alice", "aliases": []},
                    {"display_name": "Bob", "organization": "One", "profile_url": "one"},
                    {"display_name": "Bob", "organization": "Two", "profile_url": "two"},
                ],
            },
            "classification_proposals": [
                {"article_url": "https://example.test/1", "article_type": "draft"},
                {"article_url": "https://example.test/3", "article_type": "video"},
            ],
            "generated_at": self.generated_at,
        }

    def build(self):
        return build_dashboard_payload(self.payload, **self.inputs)

    def test_rejected_articles_remain_inspectable_but_not_counted_as_visible(self):
        result = self.build()
        summary = result["summary"]
        self.assertEqual((2, 1, 2), (summary["articles"], summary["rejectedArticles"], summary["reviewedArticles"]))
        self.assertEqual(3, len(result["articles"]))
        self.assertEqual(2, summary["relations"])
        self.assertEqual(1, summary["articleSummaries"])
        self.assertEqual([{"date": "2026-09-15", "count": 1}, {"date": "2026-09-16", "count": 1}], summary["dailyVolume"])
        self.assertEqual([1, 1], [talent["article_count"] for talent in result["talents"]])
        self.assertEqual(["a1"], [article["article_key"] for article in result["talentArticles"]["t1"]])

    def test_saved_classification_overrides_proposal_and_url_proposals_still_resolve(self):
        result = self.build()
        articles = {row["article_key"]: row for row in result["articles"]}
        self.assertEqual("news", articles["a1"]["classification"]["article_type"])
        self.assertEqual("video", articles["a3"]["classification"]["article_type"])
        self.assertEqual({"news": 1, "video": 1}, result["summary"]["articleTypeCounts"])
        self.assertEqual(2, result["summary"]["articleClassifications"])

    def test_official_alias_matches_but_ambiguous_name_does_not(self):
        talents = {row["talent_id"]: row for row in self.build()["talents"]}
        self.assertEqual("https://official.test/alice", talents["t1"]["officialProfileUrl"])
        self.assertNotIn("officialProfileUrl", talents["t2"])
        self.inputs["official_registry"]["talents"].pop()
        self.assertEqual("one", self.build()["talents"][1]["officialProfileUrl"])

    def test_sorting_timestamp_and_inputs_are_preserved(self):
        before = copy.deepcopy((self.payload, self.inputs))
        result = self.build()
        self.assertEqual(before, (self.payload, self.inputs))
        self.assertEqual(["a2", "a1", "a3"], [row["article_key"] for row in result["articles"]])
        self.assertEqual(["t2", "t1"], [row["talent_id"] for row in result["relations"]])
        self.assertEqual(self.generated_at.isoformat(), result["generatedAt"])

    def test_empty_data_keeps_response_shape_and_source_warning(self):
        self.payload = {"articles": [], "talents": [], "article_talents": []}
        self.inputs.update(source="proposal-files", source_error="legacy unavailable")
        result = self.build()
        self.assertEqual("legacy unavailable", result["sourceError"])
        self.assertEqual("proposal-files", result["source"])
        self.assertEqual([], result["articles"])
        self.assertEqual({}, result["talentArticles"])
        self.assertEqual(0, result["summary"]["articles"])


class WeeklyReportReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / "content/weekly-reports"
        self.directory.mkdir(parents=True)
        self.markdown = (
            "---\ntitle: 'Weekly research'\nweekEnd: 2026-09-20\n"
            "coveredThrough: 2026-09-17\n---\n# Report\n\n"
            "## Notes\n| Header |\n- First   useful line.\n"
        )
        (self.directory / "2026-09-14.md").write_text(self.markdown)
        (self.directory / "2026-09-07.md").write_text("# Older\nOlder note.\n")
        (self.directory / "notes.md").write_text("Not a dated report")

    def test_list_order_metadata_and_summary(self):
        reports = weekly.weekly_reports_payload(self.root)["reports"]
        self.assertEqual(["2026-09-14", "2026-09-07"], [row["weekStart"] for row in reports])
        self.assertEqual("Weekly research", reports[0]["title"])
        self.assertEqual("First useful line.", reports[0]["summary"])
        self.assertEqual("2026-09-17", reports[0]["coveredThrough"])

    def test_detail_returns_original_markdown_and_does_not_write(self):
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        detail = weekly.load_weekly_report(self.root, "2026-09-14")
        self.assertEqual(self.markdown, detail["markdown"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    def test_invalid_or_missing_report_is_rejected(self):
        for identifier in ("../2026-09-14", "2026-09-14.md", "/tmp/report", "2026-9-14"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                weekly.load_weekly_report(self.root, identifier)
        with self.assertRaises(FileNotFoundError):
            weekly.load_weekly_report(self.root, "2026-09-21")

    def test_summary_limit_and_empty_list(self):
        self.assertEqual("x" * 180, weekly.weekly_report_summary("# Heading\n" + "x" * 200))
        self.assertEqual("", weekly.weekly_report_summary("# Heading\n| Table |\n## Section\n"))
        self.assertEqual({"reports": []}, weekly.weekly_reports_payload(self.root / "absent"))


class DashboardCompositionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("dashboard_presentation_server", ROOT / "apps/talent-dashboard/server.py")
        self.server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.server)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch(self.server, "PROJECT_ROOT", new=self.root)
        self.patch(self.server, "load_article_summaries", return_value={})
        self.patch(self.server, "load_classification_taxonomy", return_value={})
        self.patch(self.server, "load_official_talent_registry", return_value={})
        self.patch(self.server, "load_classification_proposals", return_value=[])

    def patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_project_failure_does_not_fall_back(self):
        self.patch(self.server, "load_from_n8n", side_effect=RuntimeError("DB unavailable"))
        fallback = self.patch(self.server, "load_from_proposals")
        self.patch(project_readers, "source", return_value="project-db")
        with self.assertRaisesRegex(RuntimeError, "DB unavailable"):
            self.server.build_dashboard()
        fallback.assert_not_called()

    def test_legacy_fallback_preserves_warning(self):
        self.patch(self.server, "load_from_n8n", side_effect=RuntimeError("legacy unavailable"))
        self.patch(self.server, "load_from_proposals", return_value=(
            {"articles": [], "talents": [], "article_talents": []}, "proposal-files"
        ))
        self.patch(project_readers, "source", return_value="legacy")
        result = self.server.build_dashboard()
        self.assertEqual("proposal-files", result["source"])
        self.assertEqual("legacy unavailable", result["sourceError"])

    def test_weekly_http_routes_keep_success_and_not_found_status(self):
        handler = object.__new__(self.server.DashboardHandler)
        handler.send_json = Mock()
        handler.path = "/api/weekly-reports"
        handler.do_GET()
        self.assertEqual((200, {"reports": []}), handler.send_json.call_args.args)
        handler.path = "/api/weekly-reports/2026-09-14"
        handler.do_GET()
        self.assertEqual(404, handler.send_json.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
