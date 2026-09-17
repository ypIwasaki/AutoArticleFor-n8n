"""Source reference resolution uses recorded versions and rejects mismatches."""
import tempfile
import unittest
from pathlib import Path

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        initial = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        self.unreviewed_ref = initial['articles'][0]['ref']
        fixture.save_review(self.root)
        reviewed = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        self.ref = reviewed['articles'][0]['ref']
        self.mapping, _, _, _ = self.resolve(self.ref)

    def resolve(self, ref, day=fixture.DAY, task='article-summary'):
        with project.reader(self.root) as reader:
            return inputs.resolve(reader.c, ref, day, task)

    def save_reference(self, **changes):
        mapping = dict(self.mapping, **changes)
        reference = inputs.reference_id(mapping)
        business.submit(
            'test-reference-'+reference, 'ai-references', dict(references=[mapping]),
            project.path_for(self.root), self.root,
        )
        return reference

    def test_reviewed_reference_restores_source_and_validated_review(self):
        mapping, article, capture, review = self.resolve(self.ref)
        self.assertEqual(mapping, self.mapping)
        self.assertEqual(article['url'], fixture.URL)
        self.assertEqual(capture['contentText'], fixture.BODY)
        self.assertEqual(review['inputHash'], inputs.shared.input_hash(article, capture))
        self.assertEqual(review['taskStatus']['article-summary'], 'ready')

    def test_unreviewed_reference_does_not_pick_up_later_review(self):
        mapping, _, capture, review = self.resolve(self.unreviewed_ref)
        self.assertIsNone(mapping['reviewId'])
        self.assertIsNone(review)
        self.assertEqual(capture['contentText'], fixture.BODY)

    def test_missing_reference_wrong_day_and_wrong_task_are_rejected(self):
        for ref, day, task, message in (
            ('missing', fixture.DAY, 'article-summary', 'Unknown article reference'),
            (self.ref, '2000-01-01', 'article-summary', 'Reference belongs to another input/day'),
            (self.ref, fixture.DAY, 'talent-index', 'Reference belongs to another task'),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.resolve(ref, day, task)

    def test_missing_source_and_version_mismatches_are_rejected(self):
        invalid_mappings = (
            (dict(occurrenceId='missing'), 'Reference occurrence missing'),
            (dict(fetchAttemptId='missing'), 'Reference body mismatch'),
            (dict(contentVersionId='different-version'), 'Reference body mismatch'),
            (dict(reviewId='missing'), 'Reference review/body mismatch'),
            (dict(fetchAttemptId=None, contentVersionId='different-version'), 'Reference review/body mismatch'),
        )
        for changes, message in invalid_mappings:
            with self.subTest(changes=changes):
                reference = self.save_reference(**changes)
                with self.assertRaisesRegex(ValueError, message):
                    self.resolve(reference)

    def test_optional_capture_and_review_remain_absent(self):
        reference = self.save_reference(fetchAttemptId=None, contentVersionId=None, reviewId=None)
        _, article, capture, review = self.resolve(reference, task=None)
        self.assertEqual(article['url'], fixture.URL)
        self.assertIsNone(capture)
        self.assertIsNone(review)

    def test_old_reference_keeps_recorded_body_after_new_capture(self):
        before = self.resolve(self.ref)
        changed = dict(
            recordType='article-content', originalUrl=fixture.URL, resolvedUrl=fixture.URL,
            contentStatus='verified', contentText='Updated body', contentMarkdown='Updated body',
            contentLength=12, fetchedAt=fixture.DAY+'T02:00:00Z',
            contentCompleteness='full', contentType='article',
        )
        business.submit(
            'new-reference-test-body', 'capture', dict(day=fixture.DAY, record=changed),
            project.path_for(self.root), self.root,
        )
        self.assertEqual(self.resolve(self.ref), before)

    def additional(self, ref=None, **options):
        return inputs.additional(
            self.root, fixture.DAY, 'article-summary', ref or self.ref, **options,
        )

    def test_additional_detail_uses_review_or_unreviewed_body(self):
        reviewed = self.additional(kind='detail')
        unreviewed = self.additional(self.unreviewed_ref, kind='detail', offset=20, maximum=40)
        self.assertEqual(reviewed['state'], 'ready')
        self.assertIn('facts', reviewed)
        self.assertNotIn('content', reviewed)
        self.assertEqual(unreviewed['state'], 'needs_review')
        self.assertEqual(unreviewed['content']['text'], fixture.BODY[20:60])

    def test_additional_body_and_source_match_the_reference(self):
        body = self.additional(kind='body', offset=20, maximum=40)
        source = self.additional(kind='source')
        self.assertEqual(body['content']['text'], fixture.BODY[20:60])
        self.assertEqual(source['url'], fixture.URL)
        self.assertEqual(source['references'], self.mapping)

    def test_additional_facts_and_evidence_require_known_ids(self):
        _, _, _, review = self.resolve(self.ref)
        for kind, identifier in (('facts', 'f1'), ('evidence', 'e1')):
            with self.subTest(kind=kind):
                result = self.additional(kind=kind, ids=[identifier, identifier])
                expected = [item for item in review[kind] if item['id'] == identifier]
                self.assertEqual(result[kind], expected)
                with self.assertRaisesRegex(ValueError, 'Select fact/evidence IDs explicitly'):
                    self.additional(kind=kind)
                with self.assertRaisesRegex(ValueError, 'Unknown fact/evidence ID in this reference'):
                    self.additional(kind=kind, ids=[identifier, 'missing'])
                with self.assertRaisesRegex(ValueError, 'Unknown fact/evidence ID in this reference'):
                    self.additional(self.unreviewed_ref, kind=kind, ids=[identifier])
        with self.assertRaisesRegex(ValueError, 'Unknown reference kind'):
            self.additional(kind='unsupported')

    def test_additional_reference_without_body(self):
        reference = self.save_reference(fetchAttemptId=None, contentVersionId=None, reviewId=None)
        self.assertEqual(self.additional(reference, kind='body'), dict(ref=reference, state='no_saved_body'))
        detail = self.additional(reference, kind='detail')
        self.assertEqual(detail['contentStatus'], 'not_captured')
        self.assertEqual(detail['state'], 'needs_review')
        self.assertNotIn('content', detail)

    def test_additional_saved_status_precedes_requested_kind(self):
        business.submit(
            'additional-test-summary', 'summary',
            dict(day=fixture.DAY, summaries=[dict(ref=self.ref, text='星野アキが音楽イベントに出演する。')]),
            project.path_for(self.root), self.root,
        )
        for kind in ('detail', 'body', 'source', 'facts', 'evidence', 'unsupported'):
            with self.subTest(kind=kind):
                self.assertEqual(self.additional(kind=kind), dict(ref=self.ref, state='saved'))

    def test_additional_current_hold_precedes_historical_ready_review(self):
        review = fixture.review(self.root)
        review['taskStatus']['article-summary'] = 'held'
        review['unresolved'] = ['article-summary: event details missing']
        review['holds'] = {
            'article-summary': dict(reason='event details missing', missingTopics=['event']),
        }
        fixture.save_review(self.root, review, 'additional-test-held')
        for kind in ('detail', 'body', 'source', 'facts', 'evidence', 'unsupported'):
            with self.subTest(kind=kind):
                self.assertEqual(
                    self.additional(kind=kind),
                    dict(ref=self.ref, state='held', reason='event details missing'),
                )


if __name__ == '__main__':
    unittest.main()
