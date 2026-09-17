"""Completion must be backed by saved work and a grounded historical review."""
import json
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project
import project_database as db


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        fixture.save_review(self.root)
        with project.reader(self.root) as reader:
            _, articles = reader.load_day_articles(fixture.DAY)
            self.article_id = articles[0]['_project']['articleId']

    def submit(self, operation, kind, payload):
        return business.submit(operation, kind, payload, project.path_for(self.root), self.root)

    def test_ready_review_without_saved_work_is_not_complete(self):
        with project.reader(self.root) as reader:
            for task in inputs.TASKS:
                self.assertIsNone(inputs.completed(reader.c, self.article_id, task))

    def test_receipt_precedes_saved_summary_and_summary_is_a_fallback(self):
        packet = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        self.submit('completion-summary', 'summary', dict(
            day=fixture.DAY,
            summaries=[dict(ref=packet['articles'][0]['ref'], text='星野アキが音楽イベントに出演する。')],
        ))
        with project.reader(self.root) as reader:
            saved = inputs.completed(reader.c, self.article_id, 'article-summary')
            receipt = dict(task='article-summary', reviewId=saved['reviewId'], state='saved', ref='receipt-ref')
            with patch.object(inputs, 'history', return_value=[receipt]):
                self.assertEqual(inputs.completed(reader.c, self.article_id, 'article-summary'), receipt)
            self.assertEqual(saved['state'], 'saved')
            self.assertEqual(saved['reviewId'], receipt['reviewId'])
            summary_id = reader.c.execute('SELECT id FROM article_summaries').fetchone()[0]
            self.assertEqual(saved['artifactId'], summary_id)

    def add_legacy_proposal(self, identifier, **changes):
        # Model imported history directly inside the temporary test database.
        proposal = dict(
            article_url=fixture.URL, classification_method='ai_review',
            evidence_text=fixture.BODY.split('背景')[0],
        )
        proposal.update(changes)
        raw_json = json.dumps(proposal, ensure_ascii=False)
        with closing(db.connect(project.path_for(self.root))) as connection, db.transaction(connection):
            connection.execute(
                "INSERT INTO source_records "
                "(id,source_path,record_position,input_hash,importer_version,target_kind,target_id,state,raw_json) "
                "VALUES (?,?,?,?,?,'legacy_history_records',?,'imported',?)",
                (identifier, 'content/article-classification-proposals/'+fixture.DAY+'.json',
                 identifier, identifier, 'test', identifier, raw_json),
            )
            connection.execute(
                "INSERT INTO legacy_history_records (id,source_record_id,kind,raw_json) "
                "VALUES (?,?,'article-classification-proposals/classifications',?)",
                (identifier, identifier, raw_json),
            )

    def test_legacy_classification_proposal_can_complete_without_receipt(self):
        self.add_legacy_proposal('legacy-proposal')
        with project.reader(self.root) as reader:
            self.assertEqual(inputs.history(reader.c, 'ai-stage-completion', self.article_id), [])
            saved = inputs.completed(reader.c, self.article_id, 'article-classification')
            self.assertEqual(saved['state'], 'saved')
            self.assertEqual(saved['artifactId'], 'legacy-proposal')
            self.assertTrue(inputs.valid_review(reader.c, saved['reviewId'], self.article_id, 'article-classification'))
            self.assertIsNone(inputs.completed(reader.c, 'another-article', 'article-classification'))

    def test_legacy_proposal_requires_ai_method_and_matching_evidence(self):
        for number, changes in enumerate((
            dict(classification_method='manual'),
            dict(evidence_text=''),
            dict(evidence_text='unrelated unsupported assertion'),
            dict(article_url='https://example.test/another'),
        )):
            with self.subTest(changes=changes):
                self.add_legacy_proposal('rejected-'+str(number), **changes)
                with project.reader(self.root) as reader:
                    self.assertIsNone(inputs.completed(reader.c, self.article_id, 'article-classification'))

    def test_proposal_url_must_match_the_article_and_day(self):
        with project.reader(self.root) as reader:
            proposal = dict(article_url=fixture.URL)
            for day, article_id, expected in (
                (fixture.DAY, self.article_id, True),
                ('2000-01-01', self.article_id, False),
                (fixture.DAY, 'another-article', False),
            ):
                with self.subTest(day=day, article_id=article_id):
                    matched = inputs._proposal_matches_article(
                        reader.c, proposal,
                        'content/article-classification-proposals/' + day + '.json',
                        article_id, set(), 'article-classification',
                    )
                    self.assertEqual(matched, expected)

    def test_missing_or_wrong_article_review_is_not_valid(self):
        with project.reader(self.root) as reader:
            review_id = reader.c.execute('SELECT id FROM review_records').fetchone()[0]
            self.assertFalse(inputs.valid_review(reader.c, 'missing', self.article_id, 'article-summary'))
            self.assertFalse(inputs.valid_review(reader.c, review_id, 'another-article', 'article-summary'))


if __name__ == '__main__':
    unittest.main()
