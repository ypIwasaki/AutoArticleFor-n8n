from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import read_ai_inputs as reader


class InputReaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_jsonl(self, relative, records):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in records) + "\n", encoding="utf-8")
        return path

    def write_day(self, day="2026-09-10", count=5, schema=2, urls=None):
        urls = urls if urls is not None else [f"https://example.com/{i}" for i in range(count)]
        run = {"schemaVersion": schema, "recordType": "run", "runDate": day,
               "generatedAt": day + "T00:00:00Z", "period": {"since": "saved-start", "until": "saved-end"},
               "keywords": ["共有キーワード"], "articleCount": len(urls), "capturedArticleCount": len(urls)}
        articles = [{"schemaVersion": schema, "recordType": "article", "runDate": day,
                     "articleIndex": i + 7, "keywords": ["共有キーワード"],
                     "article": {"url": url, "title": '日本語「引用」"と改行\n🌟' + str(i),
                                 "publishedAt": day + "T00:00:00Z", "source": "媒体", "excerpt": "RSS抜粋",
                                 "matchedSearchKeywords": ["検索語"], "matchedRssKeywords": ["本文候補"]}}
                    for i, url in enumerate(urls)]
        self.write_jsonl(f"content/structured-records/{day}.jsonl", [run, *articles])
        statuses = ["verified", "partial", "metadata_only", "unavailable"]
        captures = [{"originalUrl": url, "articleKey": f"saved-{i}", "resolvedUrl": url + "/resolved",
                     "contentStatus": statuses[i % 4], "contentType": "video_metadata" if i == 2 else "article",
                     "contentText": "本文🌟\n引用\"" * 15, "contentMarkdown": "SHOULD_NOT_REPEAT",
                     "nonContentText": "SITE_NAVIGATION", "failureReason": "HTTP 403" if i == 3 else None}
                    for i, url in enumerate(urls[:4])]
        self.write_jsonl(f"content/article-body-captures/{day}.jsonl", list(reversed(captures)))
        return run, articles, captures

    def test_pagination_preserves_every_index_url_and_archived_context(self):
        run, articles, _ = self.write_day()
        received = []
        offset = 0
        while True:
            result = reader.build_payload(self.root, "2026-09-10", "article-summary", offset=offset, limit=2)
            self.assertEqual(result["totalArticles"], 5)
            received.extend((row["articleIndex"], row["url"]) for row in result["articles"])
            if offset == 0:
                self.assertEqual(result["context"]["runs"], [run])
                self.assertEqual(result["context"]["captureStatusCounts"]["not_captured"], 1)
            else:
                self.assertNotIn("context", result)
            offset = result["nextOffset"]
            if offset is None:
                break
        self.assertEqual(received, [(row["articleIndex"], row["article"]["url"]) for row in articles])

    def test_views_keep_source_provenance_without_repeated_payloads(self):
        self.write_day()
        result = reader.build_payload(self.root, "2026-09-10", "article-summary")
        row = result["articles"][0]
        self.assertEqual(row["contentStatus"], "verified")
        self.assertEqual(row["articleKey"], "saved-0")
        self.assertEqual(row["resolvedUrl"], "https://example.com/0/resolved")
        self.assertEqual(row["matchedSearchKeywords"], ["検索語"])
        self.assertNotIn("keywords", row)
        self.assertNotIn("rssExcerpt", row)
        self.assertNotIn("SHOULD_NOT_REPEAT", json.dumps(result))
        self.assertNotIn("SITE_NAVIGATION", json.dumps(result))
        keyword = reader.build_payload(self.root, "2026-09-10", "keyword-extraction")["articles"][0]
        self.assertEqual(keyword["rssExcerpt"], "RSS抜粋")
        self.assertNotIn("content", keyword)

    def test_long_body_can_be_reconstructed_exactly(self):
        _, _, captures = self.write_day(count=1)
        parts = []
        offset = 0
        while True:
            result = reader.build_payload(self.root, "2026-09-10", "talent-index",
                                          article_url=captures[0]["originalUrl"], content_offset=offset, max_content_chars=7)
            chunk = result["articles"][0]["content"]
            self.assertEqual(chunk["offset"], offset)
            self.assertLessEqual(chunk["returnedCharacters"], 7)
            parts.append(chunk["text"])
            if chunk["complete"]:
                self.assertIsNone(chunk["nextOffset"])
                break
            offset = chunk["nextOffset"]
        self.assertEqual("".join(parts), captures[0]["contentText"])

    def test_duplicate_urls_can_continue_each_selected_article_body(self):
        url = "https://example.com/shared-article"
        _, articles, captures = self.write_day(urls=[url, url])
        for article_position in (0, 1):
            with self.subTest(article_position=article_position):
                parts = []
                content_offset = 0
                while True:
                    result = reader.build_payload(
                        self.root, "2026-09-10", "article-summary",
                        article_url=url, offset=article_position, limit=1,
                        content_offset=content_offset, max_content_chars=7,
                    )
                    self.assertEqual(result["matchingArticles"], 2)
                    self.assertEqual(result["returnedArticles"], 1)
                    article = result["articles"][0]
                    self.assertEqual(article["articleIndex"], articles[article_position]["articleIndex"])
                    chunk = article["content"]
                    self.assertEqual(chunk["offset"], content_offset)
                    parts.append(chunk["text"])
                    if chunk["complete"]:
                        self.assertIsNone(chunk["nextOffset"])
                        break
                    content_offset = chunk["nextOffset"]
                self.assertEqual("".join(parts), captures[0]["contentText"])

    def test_duplicate_url_continuation_requires_single_selected_page(self):
        url = "https://example.com/shared-article"
        self.write_day(urls=[url, url])
        for offset in (0, 2):
            with self.subTest(offset=offset):
                with self.assertRaisesRegex(ValueError, "--limit 1.*--offset"):
                    reader.build_payload(
                        self.root, "2026-09-10", "article-summary",
                        article_url=url, offset=offset, content_offset=7,
                        max_content_chars=7,
                    )

    def test_missing_capture_is_distinct_from_failed_or_partial_capture(self):
        self.write_day()
        result = reader.build_payload(self.root, "2026-09-10", "article-classification")
        self.assertEqual([row["contentStatus"] for row in result["articles"]],
                         ["verified", "partial", "metadata_only", "unavailable", "not_captured"])
        self.assertEqual(result["articles"][3]["failureReason"], "HTTP 403")
        self.assertNotIn("content", result["articles"][4])

    def test_legacy_schema_and_missing_body_file(self):
        self.write_day(schema=1, count=1)
        (self.root / "content/article-body-captures/2026-09-10.jsonl").unlink()
        result = reader.build_payload(self.root, "2026-09-10", "article-summary")
        self.assertEqual(result["context"]["runs"][0]["schemaVersion"], 1)
        self.assertEqual(result["articles"][0]["contentStatus"], "not_captured")
        self.assertTrue(result["warnings"])

    def test_zero_articles_still_returns_context_and_completion(self):
        self.write_day(count=0)
        result = reader.build_payload(self.root, "2026-09-10", "article-summary")
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["totalArticles"], 0)
        self.assertIsNone(result["nextOffset"])
        self.assertIn("context", result)

    def test_weekly_missing_days_and_duplicate_urls_are_explicit(self):
        self.write_day("2026-09-07", count=1)
        self.write_day("2026-09-10", count=1)
        result = reader.build_payload(self.root, "2026-09-10", "weekly-report")
        self.assertEqual(result["coverage"]["weekStart"], "2026-09-07")
        self.assertEqual(result["coverage"]["weekEnd"], "2026-09-13")
        self.assertEqual(result["coverage"]["missingDates"], ["2026-09-08", "2026-09-09"])
        self.assertEqual(result["totalArticles"], 2)
        self.assertEqual(result["uniqueUrls"], 1)
        self.assertNotIn("content", result["articles"][0])
        empty = reader.build_payload(self.root, "2026-09-14", "weekly-report")
        self.assertEqual(empty["totalArticles"], 0)
        self.assertEqual(empty["coverage"]["missingDates"], ["2026-09-14"])

    def test_invalid_input_is_not_silently_skipped(self):
        self.write_day(count=1)
        path = self.root / "content/structured-records/2026-09-10.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write("{broken}\n")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            reader.build_payload(self.root, "2026-09-10", "article-summary")

    def test_invalid_selections_and_missing_day_fail(self):
        self.write_day(count=1)
        invalid = [{"offset": -1}, {"limit": 0}, {"offset": 2}, {"max_content_chars": 0},
                   {"content_offset": 2}, {"article_url": "https://unknown.invalid"},
                   {"article_url": "https://example.com/0", "content_offset": 9999}]
        for kwargs in invalid:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                reader.build_payload(self.root, "2026-09-10", "article-summary", **kwargs)
        with self.assertRaisesRegex(ValueError, "Missing structured"):
            reader.build_payload(self.root, "2026-09-09", "article-summary")
        with self.assertRaises(ValueError):
            reader.build_payload(self.root, "../2026-09-10", "article-summary")

    def test_cli_is_read_only_and_emits_parseable_unicode_json(self):
        self.write_day(count=1)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        output = io.StringIO()
        with patch.object(reader, "ROOT", self.root), contextlib.redirect_stdout(output):
            code = reader.main(["--run-date", "2026-09-10", "--task", "keyword-extraction"])
        self.assertEqual(code, 0)
        self.assertIn("日本語", json.loads(output.getvalue())["articles"][0]["title"])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
