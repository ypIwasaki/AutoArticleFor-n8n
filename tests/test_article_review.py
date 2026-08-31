from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from article_html_extract import semantic_html_to_markdown
from article_review_common import (
    KeywordCatalog,
    KeywordDefinition,
    default_review_block,
    keyword_matches,
    parse_review_block,
)
import generate_article_review_markdown as generator
from apply_article_markdown_reviews import read_reviews
from capture_article_contents import load_progress, save_progress
from run_article_review_history import process_is_running


class SemanticHtmlExtractionTests(unittest.TestCase):
    def test_extracts_article_structure_and_separates_site_chrome(self) -> None:
        paragraph = "ぶいすぽっ！の新しい企画について、出演者、公開日、企画の背景を説明する本文です。" * 20
        html = f"""
        <html><head><title>企画ニュース</title></head><body>
          <nav>にじさんじ メニュー ログイン</nav>
          <article>
            <header><h1>ぶいすぽっ！新企画</h1><p>執筆: テスト編集部</p></header>
            <p>{paragraph}</p>
            <h2>公開予定</h2>
            <p><a href="https://example.com/detail">公式発表</a>で詳細を確認できます。</p>
          </article>
          <footer>ホロライブ 関連記事</footer>
        </body></html>
        """
        result = semantic_html_to_markdown(html)

        self.assertEqual(result["extraction_scope"], "article")
        self.assertIn("## ぶいすぽっ！新企画", result["content_markdown"])
        self.assertIn("執筆: テスト編集部", result["plain_text"])
        self.assertIn("https://example.com/detail", result["content_markdown"])
        self.assertNotIn("にじさんじ メニュー", result["plain_text"])
        self.assertIn("にじさんじ", result["non_content_text"])
        self.assertIn(result["completeness"], {"substantial", "full"})


class KeywordGroupingTests(unittest.TestCase):
    def test_aliases_collapse_to_one_keyword_folder(self) -> None:
        catalog = KeywordCatalog.load()
        result = keyword_matches(
            title="ぶいすぽっ！のイベント情報",
            excerpt="VSPO!のイベントです",
            content_text="ぶいすぽの出演者を紹介します。",
            content_markdown="## ぶいすぽっ！イベント",
            non_content_text="",
            search_keywords=["ぶいすぽ", "VSPO!"],
            rss_keywords=["ぶいすぽっ！"],
            active_keywords=["ぶいすぽ", "VSPO!"],
            catalog=catalog,
        )

        self.assertEqual([row["keyword_id"] for row in result["matches"]], ["vspo"])
        self.assertEqual(result["assignment"], "single")
        self.assertEqual(result["primary_keywords"], ["vspo"])

    def test_exact_tie_is_kept_as_ambiguous(self) -> None:
        catalog = KeywordCatalog(
            [
                KeywordDefinition("alpha", "Alpha", ("Alpha",), "organization", 10),
                KeywordDefinition("beta", "Beta", ("Beta",), "organization", 10),
            ]
        )
        result = keyword_matches(
            title="Alpha Beta",
            excerpt="",
            content_text="",
            content_markdown="",
            non_content_text="",
            search_keywords=[],
            rss_keywords=[],
            active_keywords=["Alpha", "Beta"],
            catalog=catalog,
        )

        self.assertEqual(result["assignment"], "tie")
        self.assertEqual(set(result["primary_keywords"]), {"alpha", "beta"})


    def test_short_keyword_is_not_inferred_from_a_body_substring(self) -> None:
        catalog = KeywordCatalog(
            [KeywordDefinition("eru", "える", ("える",), "talent", 100)]
        )
        inferred = keyword_matches(
            title="ゲームの解説記事",
            excerpt="",
            content_text="誰でも理解できる内容です。",
            content_markdown="誰でも理解できる内容です。",
            non_content_text="",
            search_keywords=[],
            rss_keywords=[],
            active_keywords=["える"],
            catalog=catalog,
        )
        explicit = keyword_matches(
            title="ゲームの解説記事", excerpt="", content_text="", content_markdown="",
            non_content_text="", search_keywords=["える"], rss_keywords=[],
            active_keywords=["える"], catalog=catalog,
        )

        self.assertEqual(inferred["matches"], [])
        self.assertEqual(explicit["primary_keywords"], ["eru"])

class ReviewBlockTests(unittest.TestCase):
    def test_rejected_requires_reason(self) -> None:
        markdown = default_review_block().replace("decision: pending", "decision: rejected")
        values, errors = parse_review_block(markdown)

        self.assertEqual(values["decision"], "rejected")
        self.assertTrue(any("reason_code" in error for error in errors))


class MarkdownGenerationTests(unittest.TestCase):
    def test_generates_one_canonical_file_and_preserves_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            structured = base / "structured"
            captures = base / "captures"
            output = base / "review"
            structured.mkdir()
            captures.mkdir()
            run_date = "2026-08-25"
            records = [
                {
                    "recordType": "run",
                    "runDate": run_date,
                    "keywords": ["ぶいすぽ", "VTuber"],
                },
                {
                    "recordType": "article",
                    "runDate": run_date,
                    "keywords": ["ぶいすぽ", "VTuber"],
                    "article": {
                        "title": "ぶいすぽっ！所属VTuberの新企画",
                        "url": "https://example.com/news/1",
                        "source": "Example News",
                        "publishedAt": "2026-08-25T00:00:00Z",
                        "excerpt": "新企画の概要",
                        "matchedSearchKeywords": ["VSPO!", "Vtuber"],
                        "matchedRssKeywords": ["ぶいすぽ"],
                        "keywordMatchMethod": "rss-query",
                    },
                },
                {
                    "recordType": "article",
                    "runDate": run_date,
                    "keywords": ["VTuber"],
                    "article": {
                        "title": "本文を取得できないVTuber記事",
                        "url": "https://example.com/news/unavailable",
                        "source": "Example News",
                        "excerpt": "取得失敗時もレビュー対象にします",
                        "matchedSearchKeywords": ["VTuber"],
                        "keywordMatchMethod": "rss-query",
                    },
                },
            ]
            capture = {
                "recordType": "article-content",
                "articleKey": "article-one",
                "originalUrl": "https://example.com/news/1",
                "resolvedUrl": "https://example.com/news/1",
                "contentStatus": "verified",
                "contentCompleteness": "substantial",
                "contentText": "ぶいすぽっ！所属VTuberの企画内容を詳しく説明します。" * 20,
                "contentMarkdown": "## 企画内容\n\nぶいすぽっ！所属VTuberの企画内容を詳しく説明します。",
                "nonContentText": "にじさんじ 関連記事",
                "summary": "ぶいすぽっ！所属VTuberによる新企画の詳細を紹介する記事です。",
            }
            (structured / f"{run_date}.jsonl").write_text(
                "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records),
                encoding="utf-8",
            )
            (captures / f"{run_date}.jsonl").write_text(
                json.dumps(capture, ensure_ascii=False) + "\n", encoding="utf-8"
            )

            original_structured = generator.STRUCTURED_DIR
            original_captures = generator.CAPTURE_DIR
            try:
                generator.STRUCTURED_DIR = structured
                generator.CAPTURE_DIR = captures
                result = generator.generate(run_date, output, clean_generated=True)
                canonical = output / run_date / "articles" / "article-one.md"
                self.assertEqual(result["review_files"], 2)
                self.assertEqual(result["unavailable"], 1)
                self.assertTrue(canonical.exists())
                self.assertTrue((output / run_date / "by-keyword" / "vspo" / "article-one.md").exists())
                self.assertTrue((output / run_date / "by-keyword" / "vtuber" / "article-one.md").exists())
                canonical_files = list((output / run_date / "articles").glob("*.md"))
                self.assertEqual(len(canonical_files), 2)
                self.assertTrue(any("本文を取得できないVTuber記事" in path.read_text(encoding="utf-8") for path in canonical_files))
                self.assertTrue(all("decision: pending" in path.read_text(encoding="utf-8") for path in canonical_files))

                reviewed = canonical.read_text(encoding="utf-8").replace(
                    "decision: pending", "decision: approved"
                )
                canonical.write_text(reviewed, encoding="utf-8")
                generator.generate(run_date, output, clean_generated=True)
                self.assertIn("decision: approved", canonical.read_text(encoding="utf-8"))

                reviews, errors = read_reviews(output / run_date)
                self.assertEqual(errors, [])
                self.assertEqual(next(row for row in reviews if row["article_key"] == "article-one")["decision"], "approved")
            finally:
                generator.STRUCTURED_DIR = original_structured
                generator.CAPTURE_DIR = original_captures



class WorkflowContractTests(unittest.TestCase):
    def test_daily_workflow_preserves_keyword_provenance(self) -> None:
        workflow = json.loads(
            (ROOT / "n8n" / "workflows" / "daily-keyword-news-summary.workflow.json").read_text(
                encoding="utf-8"
            )
        )
        nodes = {node["name"]: node for node in workflow["nodes"]}
        normalize_code = nodes["Normalize and Deduplicate Articles"]["parameters"]["jsCode"]
        structured_code = nodes["Build Structured Records"]["parameters"]["jsCode"]
        self.assertIn("item.pairedItem", normalize_code)
        self.assertIn("matchedSearchKeywords", normalize_code)
        self.assertIn("mergeMetadata", normalize_code)
        self.assertIn("matchedSearchKeywords", structured_code)
        self.assertIn("searchQueries", structured_code)

    def test_feedback_workflow_accepts_markdown_reviewer_only_as_local_source(self) -> None:
        workflow = json.loads(
            (ROOT / "n8n" / "workflows" / "apply-article-feedback.workflow.json").read_text(
                encoding="utf-8"
            )
        )
        nodes = {node["name"]: node for node in workflow["nodes"]}
        validation_code = nodes["Validate Article Feedback"]["parameters"]["jsCode"]
        self.assertIn("article-review-markdown", validation_code)


class ResumeCheckpointTests(unittest.TestCase):
    def test_progress_round_trip_is_atomic(self) -> None:
        payload = {
            "schemaVersion": 1,
            "completedUrls": ["https://example.com/article"],
            "dates": {
                "2026-07-22": {
                    "completedUrls": ["https://example.com/article"],
                    "complete": False,
                }
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            progress_path = Path(temporary_directory) / "progress.json"
            save_progress(progress_path, payload)

            self.assertEqual(load_progress(progress_path), payload)
            self.assertFalse(
                progress_path.with_suffix(progress_path.suffix + ".tmp").exists()
            )

    def test_current_process_is_running(self) -> None:
        self.assertTrue(process_is_running(os.getpid()))


if __name__ == "__main__":
    unittest.main()
