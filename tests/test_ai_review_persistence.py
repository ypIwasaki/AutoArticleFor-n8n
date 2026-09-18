"""Review storage round trips, rollback, and idempotent retries in a temporary DB."""
import tempfile
import unittest
from pathlib import Path

import ai_minimization_fixture as fixture
import project_business_writes as business
import project_readers as project


class ReviewPersistenceTests(unittest.TestCase):
    TABLES = (
        'review_records', 'review_task_statuses', 'review_input_snapshots',
        'review_evidence', 'review_facts', 'review_fact_evidence',
        'review_entities', 'review_entity_facts', 'source_records',
        'article_content_versions', 'content_payloads',
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        fixture.save_review(self.root)
        self.updated = fixture.review(self.root, reviewedAt=fixture.DAY+'T02:00:00Z')

    def snapshot(self):
        with project.reader(self.root) as reader:
            return {
                table: [tuple(row) for row in reader.c.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                for table in self.TABLES
            }

    def submit(self, fault=None):
        return business.submit(
            'review-persistence-update', 'reviews',
            dict(day=fixture.DAY, records=[self.updated]),
            project.path_for(self.root), self.root, fault=fault,
        )

    def test_review_round_trip_preserves_facts_evidence_and_entities(self):
        self.submit()
        with project.reader(self.root) as reader:
            reviews, known_urls = reader.reviews(fixture.DAY, [])
        self.assertIn(fixture.URL, known_urls)
        key = (fixture.URL, self.updated['inputHash'], self.updated['policyHash'])
        self.assertEqual(dict(reviews[key][0]), self.updated)

    def test_failure_rolls_back_review_links_and_retry_succeeds(self):
        before = self.snapshot()
        def fail_mid_write(stage, index):
            if stage == 'business' and index == 8:
                raise RuntimeError('Injected review write failure')
        with self.assertRaisesRegex(RuntimeError, 'Injected review write failure'):
            self.submit(fault=fail_mid_write)
        self.assertEqual(self.snapshot(), before)
        self.submit()
        with project.reader(self.root) as reader:
            latest = reader.c.execute(
                'SELECT status,reviewed_at FROM review_records ORDER BY rowid DESC LIMIT 1',
            ).fetchone()
        # Reviews of the same unchanged inputs may both remain current.
        self.assertEqual(latest['status'], 'current')
        self.assertEqual(latest['reviewed_at'], self.updated['reviewedAt'])
        self.assertEqual(len(self.snapshot()['review_records']), 2)

    def test_replay_does_not_duplicate_review_or_links(self):
        first = self.submit()
        before = self.snapshot()
        self.assertEqual(self.submit(), first)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
