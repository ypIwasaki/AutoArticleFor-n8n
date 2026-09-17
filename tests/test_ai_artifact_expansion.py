"""Artifact expansion preserves authored inputs and existing daily records."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ai_minimization_fixture as fixture
import ai_input_minimization as inputs
import project_business_writes as business
import project_readers as project


class ArtifactExpansionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixture.setup(Path(temporary.name))
        fixture.save_review(self.root)
        self.refs = {
            task: inputs.build_payload(self.root, fixture.DAY, task)['articles'][0]['ref']
            for task in inputs.TASKS
        }

    def expand(self, request):
        with project.reader(self.root) as reader:
            return inputs.expand_artifact(SimpleNamespace(c=reader.c, root=self.root), request)

    def test_summary_expansion_preserves_request_and_text_format(self):
        request = dict(
            kind='summary', day=fixture.DAY, preamble='Introduction',
            summaries=[dict(ref=self.refs['article-summary'], text='Event summary')],
        )
        before = copy.deepcopy(request)
        result = self.expand(request)
        self.assertEqual(request, before)
        self.assertEqual(
            result['text'],
            'Introduction\n## Source-by-source Notes\n'
            '- [星野アキの音楽イベント]('+fixture.URL+') - 本文確認: 確認済み - 要約: Event summary\n',
        )

    def test_empty_summary_update_retains_saved_article(self):
        business.submit(
            'expansion-summary', 'summary',
            dict(day=fixture.DAY, summaries=[dict(ref=self.refs['article-summary'], text='Saved summary')]),
            project.path_for(self.root), self.root,
        )
        result = self.expand(dict(kind='summary', day=fixture.DAY, summaries=[]))
        self.assertIn('Saved summary', result['text'])
        self.assertIn(fixture.URL, result['text'])

    def test_proposal_resolves_article_and_relation_without_mutating_request(self):
        request = dict(
            kind='proposal', day=fixture.DAY, directory='talent-index-proposals',
            document=dict(
                articles=[dict(ref=self.refs['talent-index'], excerpt='Authored excerpt')],
                articleTalents=[dict(articleRef=self.refs['talent-index'], talent_id='test-talent')],
            ),
        )
        before = copy.deepcopy(request)
        document = self.expand(request)['document']
        self.assertEqual(request, before)
        article = document['articles'][0]
        relation = document['articleTalents'][0]
        self.assertEqual(article['url'], fixture.URL)
        self.assertEqual(article['excerpt'], 'Authored excerpt')
        self.assertNotIn('ref', article)
        self.assertNotIn('articleRef', relation)
        self.assertEqual(relation['article_key'], article['article_key'])
        self.assertEqual(relation['relation_key'], article['article_key']+'-test-talent')

    def test_conflicting_proposal_url_is_rejected(self):
        request = dict(
            kind='proposal', day=fixture.DAY, directory='article-classification-proposals',
            document=dict(classifications=[
                dict(ref=self.refs['article-classification'], article_url='https://example.test/wrong'),
            ]),
        )
        before = copy.deepcopy(request)
        with self.assertRaisesRegex(ValueError, 'Wrong source URL'):
            self.expand(request)
        self.assertEqual(request, before)


class ProposalMergeTests(unittest.TestCase):
    def test_update_retains_other_records_and_order_for_each_collection(self):
        for collection, key in (
            ('articles', 'article_key'), ('talents', 'talent_id'),
            ('articleTalents', 'relation_key'), ('classifications', 'article_url'),
            ('reviewedArticles', 'ref'),
        ):
            with self.subTest(collection=collection):
                previous = {collection: [dict([(key, 'one'), ('value', 'old')]), {key: 'two'}]}
                incoming = {collection: [dict([(key, 'one'), ('value', 'new')]), {key: 'three'}]}
                unchanged = copy.deepcopy(previous)
                inputs._merge_proposal_records(previous, incoming)
                self.assertEqual(previous, unchanged)
                self.assertEqual([item[key] for item in incoming[collection]], ['one', 'two', 'three'])
                self.assertEqual(incoming[collection][0]['value'], 'new')

    def test_empty_update_retains_records_without_adding_absent_collections(self):
        previous = dict(articles=[dict(article_key='saved')])
        incoming = dict(articles=[])
        inputs._merge_proposal_records(previous, incoming)
        self.assertEqual(incoming, previous)

    def test_fallback_key_and_missing_identity(self):
        incoming = dict(reviewedArticles=[dict(article_key='legacy')])
        inputs._merge_proposal_records({}, incoming)
        self.assertEqual(incoming['reviewedArticles'], [dict(article_key='legacy')])
        with self.assertRaisesRegex(ValueError, 'Proposal merge requires an explicit stable key'):
            inputs._merge_proposal_records({}, dict(articles=[dict(title='Missing identity')]))


if __name__ == '__main__':
    unittest.main()
