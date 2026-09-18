"""Immutable history identities and grounded hold assessment persistence."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project


class HistoryIdentityTests(unittest.TestCase):
    def test_identity_uses_explicit_key_or_content_hash_and_preserves_input(self):
        value = dict(articleId='article', message='履歴')
        original = copy.deepcopy(value)
        for key in (None, '', 'explicit-key'):
            with self.subTest(key=key), patch.object(business, 'provenance') as provenance:
                unit = SimpleNamespace(save=Mock())
                inputs.save_history(unit, 'test-history', value, 'article', key)
                identity_key = key or inputs.shared.digest(value)
                source = business.source('ai-input/test-history', identity_key, value)
                identifier = inputs.record_values.record_id('test-history', identity_key)
                provenance.assert_called_once_with(unit, source, 'legacy_history_records', identifier)
                unit.save.assert_called_once_with('history', dict(
                    id=identifier,
                    source_record_id=inputs.record_values.record_id(
                        'source', source['path'], source['position'], source['input_hash'],
                    ),
                    kind='test-history', article_id='article', talent_id=None,
                    raw_json=inputs.db.canonical(value),
                ), source)
        self.assertEqual(value, original)


class HoldAssessmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        fixture.save_review(self.root)
        with project.reader(self.root) as reader:
            self.ready_review_id = reader.c.execute('SELECT id FROM review_records').fetchone()[0]
        held = fixture.review(self.root)
        held['taskStatus']['talent-index'] = 'held'
        held['unresolved'] = ['talent-index: 星野アキの契約期間が不明']
        fixture.save_review(self.root, held, 'history-test-held')
        with project.reader(self.root) as reader:
            article_id = reader.c.execute('SELECT article_id FROM review_records LIMIT 1').fetchone()[0]
            self.assessment = inputs.history(reader.c, 'ai-legacy-hold-assessment', article_id)[-1]

    def save(self, assessment):
        with project.reader(self.root) as reader:
            inputs.save_hold_assessment(SimpleNamespace(c=reader.c, root=self.root), assessment)

    def test_matching_assessment_reaches_history_save_unchanged(self):
        original = copy.deepcopy(self.assessment)
        with patch.object(inputs, 'save_history') as save:
            self.save(self.assessment)
            save.assert_called_once()
            self.assertEqual(save.call_args[0][1:], (
                'ai-legacy-hold-assessment', original, original['articleId'],
            ))
        self.assertEqual(self.assessment, original)

    def test_missing_or_wrong_article_sources_prevent_save(self):
        for field in ('heldReviewId', 'currentReviewId', 'articleId'):
            with self.subTest(field=field), patch.object(inputs, 'save_history') as save:
                invalid = dict(self.assessment, **{field: 'missing'})
                with self.assertRaisesRegex(ValueError, 'Hold assessment source missing'):
                    self.save(invalid)
                save.assert_not_called()

    def test_review_without_hold_prevents_save(self):
        invalid = dict(self.assessment, heldReviewId=self.ready_review_id)
        with patch.object(inputs, 'save_history') as save:
            with self.assertRaisesRegex(ValueError, 'Hold assessment needs a historical hold'):
                self.save(invalid)
            save.assert_not_called()

    def test_changed_assessment_is_rejected_after_recalculation(self):
        invalid = dict(self.assessment, explanation='Unsupported replacement explanation')
        with patch.object(inputs, 'save_history') as save:
            with self.assertRaisesRegex(ValueError, 'Hold assessment differs from grounded inputs'):
                self.save(invalid)
            save.assert_not_called()


if __name__ == '__main__':
    unittest.main()
