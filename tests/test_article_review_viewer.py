from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from article_review_common import parse_review_block
from article_review_viewer import (
    ReviewConflictError,
    ReviewRequestHandler,
    ReviewServer,
    ReviewStore,
    ReviewStoreError,
    safe_web_url,
    youtube_embed_url,
    youtube_video_id,
)


ARTICLE_KEY = "a" * 64


def sample_markdown() -> str:
    return """---
schema_version: 1
article_key: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
run_date: "2026-08-26"
title: "ホロライブのテスト記事"
original_url: "https://example.com/article"
source_domain: "example.com"
publisher_label: "Example News"
published_at: "2026-08-26T01:00:00Z"
content_status: "verified"
content_completeness: "full"
content_length: 321
keyword_assignment: "single"
matched_keyword_ids:
  - "hololive"
primary_keyword_ids:
  - "hololive"
---

# ホロライブのテスト記事

- 元記事: [https://example.com/article](https://example.com/article)

## 内容の概要

これは記事の概要です。

## 抽出本文

本文を保持して評価欄だけを更新します。

## ユーザーレビュー

<!-- article-review:start -->
~~~yaml
decision: pending
keyword_check: pending
content_check: pending
reason_code: ""
note: ""
reviewed_at: ""
~~~
<!-- article-review:end -->
"""


class ReviewViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.review_root = Path(self.temporary.name) / "article-review"
        date_dir = self.review_root / "2026-08-26"
        article_dir = date_dir / "articles"
        keyword_dir = date_dir / "by-keyword" / "hololive"
        article_dir.mkdir(parents=True)
        keyword_dir.mkdir(parents=True)
        self.article_path = article_dir / f"{ARTICLE_KEY}.md"
        self.article_path.write_text(sample_markdown(), encoding="utf-8")
        (keyword_dir / "index.md").write_text(
            "# キーワード: ホロライブ\n\n- 記事数: 1\n", encoding="utf-8"
        )
        self.store = ReviewStore(self.review_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lists_dates_articles_and_safe_detail_body(self) -> None:
        self.assertEqual(
            self.store.dates(),
            [{"date": "2026-08-26", "article_count": 1}],
        )
        payload = self.store.articles("2026-08-26")
        self.assertEqual(len(payload["articles"]), 1)
        self.assertEqual(payload["keywords"], [{"id": "hololive", "label": "ホロライブ"}])
        summary = payload["articles"][0]
        self.assertEqual(summary["decision"], "pending")
        self.assertEqual(summary["matched_keywords"][0]["label"], "ホロライブ")

        detail = self.store.article("2026-08-26", ARTICLE_KEY)
        self.assertIn("## 抽出本文", detail["body_markdown"])
        self.assertNotIn("article_key:", detail["body_markdown"])
        self.assertNotIn("article-review:start", detail["body_markdown"])

    def test_updates_only_review_block_atomically(self) -> None:
        before = self.article_path.read_text(encoding="utf-8")
        detail = self.store.article("2026-08-26", ARTICLE_KEY)
        updated = self.store.update_review(
            "2026-08-26",
            ARTICLE_KEY,
            {
                "decision": "rejected",
                "keyword_check": "incorrect",
                "content_check": "correct",
                "reason_code": "irrelevant",
                "note": "対象キーワードとは無関係",
                "expected_mtime_ns": detail["mtime_ns"],
            },
        )

        after = self.article_path.read_text(encoding="utf-8")
        review, errors = parse_review_block(after)
        self.assertEqual(errors, [])
        self.assertEqual(review["decision"], "rejected")
        self.assertEqual(review["reason_code"], "irrelevant")
        self.assertEqual(review["note"], "対象キーワードとは無関係")
        self.assertTrue(review["reviewed_at"])
        self.assertIn("本文を保持して評価欄だけを更新します。", after)
        self.assertEqual(
            before.split("<!-- article-review:start -->")[0],
            after.split("<!-- article-review:start -->")[0],
        )
        self.assertEqual(updated["decision"], "rejected")
        self.assertFalse(self.article_path.with_suffix(".md.tmp").exists())

    def test_rejected_requires_reason(self) -> None:
        with self.assertRaisesRegex(ReviewStoreError, "理由"):
            self.store.update_review(
                "2026-08-26",
                ARTICLE_KEY,
                {
                    "decision": "rejected",
                    "keyword_check": "pending",
                    "content_check": "pending",
                    "reason_code": "",
                    "note": "",
                },
            )

    def test_detects_stale_markdown(self) -> None:
        with self.assertRaisesRegex(ReviewConflictError, "更新"):
            self.store.update_review(
                "2026-08-26",
                ARTICLE_KEY,
                {
                    "decision": "approved",
                    "keyword_check": "correct",
                    "content_check": "correct",
                    "reason_code": "",
                    "note": "",
                    "expected_mtime_ns": "1",
                },
            )

    def test_rejects_unsafe_path_components(self) -> None:
        with self.assertRaises(ReviewStoreError):
            self.store.article("../2026-08-26", ARTICLE_KEY)
        with self.assertRaises(ReviewStoreError):
            self.store.article("2026-08-26", "../article")

    def test_accepts_only_http_urls(self) -> None:
        self.assertEqual(safe_web_url("https://example.com/article"), "https://example.com/article")
        self.assertEqual(safe_web_url("javascript:alert(1)"), "")
        self.assertEqual(safe_web_url("file:///etc/passwd"), "")
        self.assertEqual(safe_web_url("not-a-url"), "")

    def test_recognizes_youtube_url_forms(self) -> None:
        video_id = "bsv2lsEzqyk"
        urls = [
            f"https://www.youtube.com/watch?v={video_id}",
            f"https://youtu.be/{video_id}?si=example",
            f"https://www.youtube.com/shorts/{video_id}",
            f"https://www.youtube.com/live/{video_id}",
            f"https://www.youtube-nocookie.com/embed/{video_id}",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(youtube_video_id(url), video_id)
        self.assertEqual(youtube_video_id("https://example.com/watch?v=bsv2lsEzqyk"), "")
        self.assertEqual(
            youtube_embed_url(urls[0]),
            "https://www.youtube-nocookie.com/embed/bsv2lsEzqyk?rel=0",
        )

    def test_youtube_detail_includes_saved_video_text(self) -> None:
        markdown = sample_markdown()
        markdown = markdown.replace(
            'original_url: "https://example.com/article"',
            'original_url: "https://www.youtube.com/watch?v=bsv2lsEzqyk"',
        )
        markdown = markdown.replace(
            "これは記事の概要です。",
            "保存済みの動画概要です。\n\n二行目も表示します。",
        )
        markdown = markdown.replace(
            "本文を保持して評価欄だけを更新します。",
            "タイトル: 動画タイトル\n投稿者・チャンネル: テストチャンネル\n配信元: YouTube",
        )
        self.article_path.write_text(markdown, encoding="utf-8")

        detail = self.store.article("2026-08-26", ARTICLE_KEY)
        self.assertTrue(detail["is_youtube"])
        self.assertEqual(
            detail["embed_url"],
            "https://www.youtube-nocookie.com/embed/bsv2lsEzqyk?rel=0",
        )
        self.assertEqual(detail["video_metadata"]["title"], "動画タイトル")
        self.assertEqual(detail["video_metadata"]["channel"], "テストチャンネル")
        self.assertIn("二行目", detail["video_metadata"]["description"])


class ReviewViewerHttpTests(unittest.TestCase):
    def test_health_and_dates_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            review_root = Path(temporary) / "article-review"
            (review_root / "2026-08-26" / "articles").mkdir(parents=True)
            server = ReviewServer(
                ("127.0.0.1", 0),
                ReviewRequestHandler,
                ReviewStore(review_root),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with request.urlopen(base + "/api/health", timeout=5) as response:
                    self.assertEqual(json.load(response), {"ok": True})
                with request.urlopen(base + "/api/dates", timeout=5) as response:
                    payload = json.load(response)
                self.assertEqual(payload["default_date"], "2026-08-26")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
