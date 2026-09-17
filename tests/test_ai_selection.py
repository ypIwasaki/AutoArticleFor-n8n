"""Selection precedence and evidence identity over an isolated project DB."""
import copy
import tempfile
import unittest
from pathlib import Path

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project


class SelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))

    def select(self, task='article-summary', capture_changes=None, policy=None):
        with project.reader(self.root) as reader:
            _, articles, captures = reader.load_day(fixture.DAY, [])
            capture = captures[fixture.URL]
            if capture_changes:
                capture.update(capture_changes)
            return inputs.select(
                reader.c, fixture.DAY, articles[0], capture, task,
                policy or inputs.shared.policy_hash(self.root),
            )

    def test_ready_review_is_not_saved_work(self):
        fixture.save_review(self.root)
        result = self.select()
        self.assertEqual(result['state'], 'ready')
        self.assertIsNotNone(result['record'])
        self.assertIsNotNone(result['reviewId'])

    def test_changed_policy_requires_review(self):
        fixture.save_review(self.root)
        result = self.select(policy='changed-policy')
        self.assertEqual(result, dict(state='needs_review', record=None, reviewId=None))

    def test_unavailable_excludes_only_summary_and_classification(self):
        for task in inputs.TASKS:
            with self.subTest(task=task):
                result = self.select(task, dict(contentStatus='unavailable', failureReason='HTTP 404'))
                if task == 'talent-index':
                    self.assertEqual(result['state'], 'needs_review')
                else:
                    self.assertEqual(result, dict(state='unavailable', reason='HTTP 404'))

    def test_hold_takes_precedence_over_unavailable(self):
        review = fixture.review(self.root)
        review['taskStatus']['article-summary'] = 'held'
        review['unresolved'] = ['article-summary: event details missing']
        review['holds'] = {
            'article-summary': dict(reason='event details missing', missingTopics=['event']),
        }
        fixture.save_review(self.root, review)
        result = self.select(capture_changes=dict(contentStatus='unavailable', failureReason='HTTP 404'))
        self.assertEqual(result, dict(state='held', reason='event details missing'))

    def test_saved_work_takes_precedence_over_current_capture(self):
        fixture.save_review(self.root)
        packet = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        business.submit(
            'selection-test-summary', 'summary',
            dict(day=fixture.DAY, summaries=[
                dict(ref=packet['articles'][0]['ref'], text='星野アキが音楽イベントに出演する。'),
            ]),
            project.path_for(self.root), self.root,
        )
        result = self.select(capture_changes=dict(contentStatus='unavailable', failureReason='HTTP 404'))
        self.assertEqual(result['state'], 'saved')
        self.assertEqual(result['receipt']['task'], 'article-summary')

    def test_unsaved_article_rejects_capture_from_another_article(self):
        with self.assertRaisesRegex(ValueError, 'Article/capture identity mismatch'):
            self.select(capture_changes=dict(_project=dict(articleId='another-article')))

    def test_material_ignores_identifiers_but_detects_changed_quotes(self):
        original = fixture.review(self.root)
        original['facts'][0]['topics'] = ['event']
        renamed = copy.deepcopy(original)
        renamed['evidence'][0]['id'] = 'renamed-evidence'
        renamed['facts'][0].update(id='renamed-fact', evidenceIds=['renamed-evidence'])
        expected = inputs.material(original, 'article-summary', ['event'])
        self.assertEqual(len(expected), 1)
        self.assertEqual(inputs.material(renamed, 'article-summary', ['event']), expected)
        self.assertEqual(inputs.material(original, 'article-summary', ['contract']), set())
        renamed['evidence'][0]['quote'] += ' additional evidence'
        self.assertNotEqual(inputs.material(renamed, 'article-summary', ['event']), expected)


if __name__ == '__main__':
    unittest.main()
