"""Explicit proposal results must stay bound to the reviewed article."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_readers as project


class ArtifactRecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        fixture.save_review(self.root)
        self.refs = {
            task: inputs.build_payload(self.root, fixture.DAY, task)['articles'][0]['ref']
            for task in inputs.TASKS
        }

    def request(self, task, result, url=None):
        talent = task == 'talent-index'
        collection = 'articles' if talent else 'classifications'
        url_field = 'url' if talent else 'article_url'
        return dict(
            kind='proposal', day=fixture.DAY,
            directory='talent-index-proposals' if talent else 'article-classification-proposals',
            document={
                'reviewedArticles': [dict(ref=self.refs[task], result=result)],
                collection: [{url_field: url}] if url is not None else [],
            },
        )

    def record(self, request):
        with project.reader(self.root) as reader:
            inputs.record_artifact(SimpleNamespace(c=reader.c, root=self.root), request)

    def test_confirmed_result_records_matching_article_and_document_hash(self):
        for task in ('talent-index', 'article-classification'):
            with self.subTest(task=task), patch.object(inputs, 'completion') as complete:
                request = self.request(task, 'confirmed', fixture.URL)
                self.record(request)
                complete.assert_called_once()
                _, reference, day, recorded_task, artifact = complete.call_args[0]
                self.assertEqual((reference, day, recorded_task), (self.refs[task], fixture.DAY, task))
                self.assertEqual(artifact, dict(
                    kind='proposal', directory=request['directory'],
                    documentHash=inputs.shared.digest(request['document']), result='confirmed',
                ))

    def test_confirmed_result_requires_matching_proposal(self):
        for task in ('talent-index', 'article-classification'):
            for url in (None, 'https://example.test/wrong'):
                with self.subTest(task=task, url=url), patch.object(inputs, 'completion') as complete:
                    with self.assertRaisesRegex(ValueError, 'Reviewed article missing from proposal'):
                        self.record(self.request(task, 'confirmed', url))
                    complete.assert_not_called()

    def test_explicit_negative_result_does_not_require_a_proposal_row(self):
        for task in ('talent-index', 'article-classification'):
            with self.subTest(task=task), patch.object(inputs, 'completion') as complete:
                self.record(self.request(task, 'none'))
                complete.assert_called_once()
                self.assertEqual(complete.call_args[0][-1]['result'], 'none')

    def test_invalid_results_do_not_record_completion(self):
        for result in (None, '', 'ready', 'held'):
            with self.subTest(result=result), patch.object(inputs, 'completion') as complete:
                with self.assertRaisesRegex(ValueError, 'Explicit confirmed/none result required'):
                    self.record(self.request('talent-index', result))
                complete.assert_not_called()

    def test_empty_document_does_not_imply_completion(self):
        for task in ('talent-index', 'article-classification'):
            with self.subTest(task=task), patch.object(inputs, 'completion') as complete:
                request = self.request(task, 'none')
                request['document']['reviewedArticles'] = []
                self.record(request)
                complete.assert_not_called()

    def test_wrong_task_reference_is_rejected(self):
        request = self.request('talent-index', 'none')
        request['document']['reviewedArticles'][0]['ref'] = self.refs['article-classification']
        with patch.object(inputs, 'completion') as complete:
            with self.assertRaisesRegex(ValueError, 'Reference belongs to another task'):
                self.record(request)
            complete.assert_not_called()


if __name__ == '__main__':
    unittest.main()
