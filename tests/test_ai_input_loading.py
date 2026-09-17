"""Verify selective body reads against isolated normalized records."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project


class InputLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.other_url = 'https://example.test/other-article'
        submit_collection = business.submit_collection

        def submit_two_articles(operation_id, records, database, root):
            run, original = records
            run = dict(run, capturedArticleCount=2)
            other = dict(
                original, articleIndex=2,
                article=dict(original['article'], url=self.other_url),
            )
            return submit_collection(operation_id, [run, original, other], database, root)

        with patch.object(business, 'submit_collection', submit_two_articles):
            self.root = fixture.setup(Path(temporary.name))
        with project.reader(self.root) as reader:
            _, _, captures = reader.load_day(fixture.DAY, [])
        capture = {key: value for key, value in captures[fixture.URL].items() if key != '_project'}
        capture.update(originalUrl=self.other_url, resolvedUrl=self.other_url)
        business.submit(
            'other-capture', 'capture', dict(day=fixture.DAY, record=capture),
            project.path_for(self.root), self.root,
        )

    def test_filtered_capture_matches_full_read(self):
        with project.reader(self.root) as reader:
            expected = reader.load_day(fixture.DAY, [])
            with patch.object(reader, 'capture', wraps=reader.capture) as capture:
                actual = reader.load_day(fixture.DAY, [], capture_urls={fixture.URL})
            self.assertEqual(capture.call_count, 1)
        self.assertEqual(actual[:2], expected[:2])
        self.assertEqual(actual[2], {fixture.URL: expected[2][fixture.URL]})
        self.assertEqual(len(expected[2]), 2)

    def test_empty_filter_reads_no_bodies(self):
        with project.reader(self.root) as reader:
            with patch.object(reader, 'capture', side_effect=AssertionError('Unexpected body read')):
                _, articles, captures = reader.load_day(fixture.DAY, [], capture_urls=set())
        self.assertEqual(len(articles), 2)
        self.assertEqual(captures, {})

    def test_saved_status_reads_no_bodies(self):
        with patch.object(project.Reader, 'capture', side_effect=AssertionError('Unexpected body read')):
            saved = inputs.saved_for_day(self.root, fixture.DAY, 'article-summary')
        self.assertEqual(saved, {fixture.URL: None, self.other_url: None})

    def test_targeted_packet_matches_full_packet(self):
        full = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        original_capture = project.Reader.capture
        captured_urls = []

        def track_capture(reader, row):
            captured_urls.append(row['original_url'])
            return original_capture(reader, row)

        with patch.object(project.Reader, 'capture', track_capture):
            targeted = inputs.build_payload(
                self.root, fixture.DAY, 'article-summary', article_url=fixture.URL,
            )
        self.assertEqual(captured_urls, [fixture.URL])
        self.assertEqual(targeted['articles'], full['articles'][:1])
        self.assertEqual(targeted['totalArticles'], 2)
        self.assertEqual(targeted['matchingArticles'], 1)

    def test_unknown_url_reads_no_bodies(self):
        with patch.object(project.Reader, 'capture', side_effect=AssertionError('Unexpected body read')):
            packet = inputs.build_payload(
                self.root, fixture.DAY, 'article-summary', article_url='https://example.test/missing',
            )
        self.assertEqual(packet['articles'], [])
        self.assertEqual(packet['totalArticles'], 2)

    def test_policy_is_read_once_per_packet(self):
        with patch.object(inputs.shared, 'policy_hash', wraps=inputs.shared.policy_hash) as policy:
            inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        self.assertEqual(policy.call_count, 1)

    def test_pages_preserve_order_totals_and_end_boundary(self):
        full = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        first = inputs.build_payload(self.root, fixture.DAY, 'article-summary', limit=1)
        second = inputs.build_payload(self.root, fixture.DAY, 'article-summary', offset=1, limit=1)
        end = inputs.build_payload(self.root, fixture.DAY, 'article-summary', offset=2, limit=1)
        self.assertEqual(first['articles'] + second['articles'], full['articles'])
        self.assertEqual(first['nextOffset'], 1)
        self.assertIsNone(second['nextOffset'])
        self.assertEqual(end['articles'], [])
        self.assertIsNone(end['nextOffset'])
        for page in (first, second, end):
            self.assertEqual(page['totalArticles'], 2)
            self.assertEqual(page['matchingArticles'], 2)

    def test_invalid_page_never_saves_references(self):
        invalid_options = (
            dict(offset=-1), dict(limit=0), dict(content_offset=-1),
            dict(max_content_chars=0), dict(offset=3), dict(content_offset=1),
            dict(article_url=fixture.URL, offset=1, content_offset=1),
        )
        with patch.object(business, 'submit') as submit:
            for options in invalid_options:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    inputs.build_payload(self.root, fixture.DAY, 'article-summary', **options)
            submit.assert_not_called()

    def test_reviewed_body_is_only_included_when_requested(self):
        fixture.save_review(self.root)
        options = dict(article_url=fixture.URL, max_content_chars=80)
        compact = inputs.build_payload(self.root, fixture.DAY, 'article-summary', **options)
        expanded = inputs.build_payload(
            self.root, fixture.DAY, 'article-summary', include_body=True, **options,
        )
        compact_article = compact['articles'][0]
        expanded_article = expanded['articles'][0]
        self.assertNotIn('content', compact_article)
        self.assertNotIn('excerpt', compact_article)
        self.assertIn('content', expanded_article)
        self.assertEqual(
            {key: value for key, value in expanded_article.items() if key not in ('content', 'excerpt')},
            compact_article,
        )

    def test_review_validation_still_uses_body(self):
        fixture.save_review(self.root)
        packet = inputs.build_payload(
            self.root, fixture.DAY, 'article-summary', article_url=fixture.URL,
        )
        self.assertEqual(packet['articles'][0]['state'], 'ready')
        with project.reader(self.root) as reader:
            _, _, captures = reader.load_day(fixture.DAY, [])
        changed = {key: value for key, value in captures[fixture.URL].items() if key != '_project'}
        changed.update(contentText='Changed body', contentMarkdown='Changed body', contentLength=12)
        business.submit(
            'changed-capture', 'capture', dict(day=fixture.DAY, record=changed),
            project.path_for(self.root), self.root,
        )
        packet = inputs.build_payload(
            self.root, fixture.DAY, 'article-summary', article_url=fixture.URL,
        )
        self.assertEqual(packet['articles'][0]['state'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
