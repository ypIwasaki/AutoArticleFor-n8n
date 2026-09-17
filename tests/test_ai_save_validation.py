"""Source-bound review expansion and validation before completion writes."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project


class SaveValidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        initial = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        self.unreviewed_ref = initial['articles'][0]['ref']
        fixture.save_review(self.root)
        reviewed = inputs.build_payload(self.root, fixture.DAY, 'article-summary')
        self.ref = reviewed['articles'][0]['ref']

    def expand(self, authored):
        with project.reader(self.root) as reader:
            return inputs.expand_review(reader.c, fixture.DAY, authored)

    def complete(self, reference=None):
        with project.reader(self.root) as reader:
            unit = SimpleNamespace(c=reader.c, root=self.root)
            inputs.completion(
                unit, reference or self.ref, fixture.DAY, 'article-summary', dict(kind='summary'),
            )

    def test_review_without_reference_is_unchanged(self):
        authored = fixture.review(self.root)
        self.assertIs(self.expand(authored), authored)

    def test_reference_fills_source_fields_without_mutating_authored_review(self):
        expected = fixture.review(self.root)
        authored = copy.deepcopy(expected)
        for field in ('url', 'inputHash', 'policyHash', 'reviewVersion'):
            del authored[field]
        authored['ref'] = self.ref
        before = copy.deepcopy(authored)
        self.assertEqual(self.expand(authored), expected)
        self.assertEqual(authored, before)

    def test_reference_rejects_conflicting_source_fields(self):
        for field in ('url', 'inputHash', 'policyHash', 'reviewVersion'):
            with self.subTest(field=field):
                authored = dict(ref=self.ref, **{field: 'conflicting-value'})
                with self.assertRaisesRegex(ValueError, 'Authored reference disagrees with source'):
                    self.expand(authored)

    def test_completion_records_exact_reviewed_reference(self):
        with project.reader(self.root) as reader:
            mapping, _, _, _ = inputs.resolve(reader.c, self.ref, fixture.DAY)
        with patch.object(inputs, 'save_history') as save:
            self.complete()
        save.assert_called_once()
        _, kind, receipt, article_id = save.call_args[0]
        self.assertEqual(kind, 'ai-stage-completion')
        self.assertEqual(article_id, mapping['articleId'])
        self.assertEqual(receipt, dict(
            task='article-summary', articleId=mapping['articleId'],
            reviewId=mapping['reviewId'], contentVersionId=mapping['contentVersionId'],
            ref=self.ref, artifact=dict(kind='summary'), state='saved',
        ))

    def test_unreviewed_completion_is_rejected_without_saving(self):
        with patch.object(inputs, 'save_history') as save:
            with self.assertRaisesRegex(ValueError, 'Saved artifact requires reviewed ready evidence'):
                self.complete(self.unreviewed_ref)
            save.assert_not_called()

    def test_changed_policy_rejects_new_completion_without_saving(self):
        policy_file = self.root / inputs.shared.RULES
        policy_file.write_text(policy_file.read_text() + '\nTest policy update\n')
        with patch.object(inputs, 'save_history') as save:
            with self.assertRaises(ValueError):
                self.complete()
            save.assert_not_called()

    def test_saved_review_rejects_reauthoring_and_needs_no_new_receipt(self):
        business.submit(
            'save-validation-summary', 'summary',
            dict(day=fixture.DAY, summaries=[dict(ref=self.ref, text='星野アキが音楽イベントに出演する。')]),
            project.path_for(self.root), self.root,
        )
        with self.assertRaisesRegex(ValueError, 'Stage already saved'):
            self.expand(dict(ref=self.ref))
        policy_file = self.root / inputs.shared.RULES
        policy_file.write_text(policy_file.read_text() + '\nTest policy update\n')
        with patch.object(inputs, 'save_history') as save:
            self.complete()
            save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
