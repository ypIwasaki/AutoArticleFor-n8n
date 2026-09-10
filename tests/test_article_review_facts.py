from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import article_review_facts as shared
import read_ai_inputs as reader
import save_article_review_facts as writer
import test_read_ai_inputs as fixtures


class SharedReviewTests(unittest.TestCase):
    write_jsonl = fixtures.InputReaderTests.write_jsonl
    write_day = fixtures.InputReaderTests.write_day

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in shared.POLICY_FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('shared policy v1', encoding='utf-8')
        self.day = '2026-09-10'
        self.run, self.articles, self.captures = self.write_day(count=1)
        self.path = self.root / shared.DIRECTORY / (self.day + '.jsonl')

    def review(self):
        article, capture = self.articles[0]['article'], self.captures[0]
        return {'reviewVersion': 1, 'url': article['url'],
                'inputHash': shared.input_hash(article, capture), 'policyHash': shared.policy_hash(self.root),
                'reviewedBy': 'codex-test', 'basis': 'body',
                'taskStatus': {task: 'ready' for task in shared.TASKS},
                'facts': [{'id': 'f1', 'text': '本文の事実', 'evidenceIds': ['e1']}],
                'entities': [{'name': '対象', 'kind': 'other', 'factIds': ['f1']}],
                'evidence': [{'id': 'e1', 'field': 'contentText', 'start': 0, 'end': 3, 'quote': capture['contentText'][:3]}],
                'unresolved': []}

    def view(self, task='article-summary', **kwargs):
        return reader.build_payload(self.root, self.day, task, **kwargs)['articles'][0]

    def save(self, record=None):
        return writer.save_reviews(self.root, self.day, record or self.review())

    def test_missing_review_never_promotes_capture_to_semantic_ready(self):
        view = self.view()
        self.assertEqual(view['contentStatus'], 'verified')
        self.assertEqual(view['sharedReview']['status'], 'missing')
        self.assertEqual(view['sharedReview']['taskStatus'], 'needs_review')
        self.assertIn('content', view)
        self.assertFalse(self.path.exists())

    def test_reuse_across_three_tasks_and_explicit_body_override(self):
        self.save()
        for task in shared.TASKS:
            view = self.view(task)
            self.assertEqual(view['sharedReview']['status'], 'current')
            self.assertTrue(view['bodyOmitted'])
            self.assertNotIn('content', view)
            self.assertEqual(view['sharedReview']['record']['facts'][0]['id'], 'f1')
        explicit = self.view(include_body=True)
        self.assertFalse(explicit['bodyOmitted'])
        self.assertIn('content', explicit)
        continuation = self.view(article_url=self.captures[0]['originalUrl'], content_offset=7)
        self.assertEqual(continuation['content']['text'], self.captures[0]['contentText'][7:])

    def test_only_incomplete_task_reads_body(self):
        record = self.review()
        record['taskStatus']['talent-index'] = 'needs_review'
        record['unresolved'] = ['talent-index: 人物一覧が未確認']
        self.save(record)
        self.assertNotIn('content', self.view('article-summary'))
        self.assertIn('content', self.view('talent-index'))

    def test_policy_change_invalidates_review(self):
        self.save()
        (self.root / shared.RULES).write_text('shared policy v2', encoding='utf-8')
        view = self.view()
        self.assertEqual(view['sharedReview']['status'], 'stale')
        self.assertIn('content', view)

    def test_source_changes_invalidate_review(self):
        self.save()
        for key in ('title', 'excerpt', 'publishedAt', 'source'):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.articles)
                changed[0]['article'][key] += ' changed'
                self.write_jsonl(f'content/structured-records/{self.day}.jsonl', [self.run, *changed])
                self.assertEqual(self.view()['sharedReview']['status'], 'stale')

        self.write_jsonl(f'content/structured-records/{self.day}.jsonl', [self.run, *self.articles])
        for key in ('contentText', 'resolvedUrl', 'contentStatus', 'contentMarkdown'):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.captures)
                changed[0][key] += ' changed'
                self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl', changed)
                self.assertEqual(self.view()['sharedReview']['status'], 'stale')

    def test_task_rule_change_invalidates_but_taxonomy_change_preserves_facts(self):
        self.save()
        config = self.root / 'config/article-classification-taxonomy.json'
        config.parent.mkdir()
        config.write_text('{"newTaxonomy": true}', encoding='utf-8')
        self.assertEqual(self.view()['sharedReview']['status'], 'current')
        (self.root / shared.POLICY_FILES[1]).write_text('changed task policy', encoding='utf-8')
        self.assertEqual(self.view()['sharedReview']['status'], 'stale')

    def test_invalid_evidence_and_references_rejected_without_writes(self):
        self.save()
        original = self.path.read_bytes()
        for mutation in ('quote', 'range', 'fact-reference', 'entity-reference', 'empty-facts', 'timestamp-as-ready'):
            record = self.review()
            if mutation == 'quote': record['evidence'][0]['quote'] = '不存在'
            elif mutation == 'range': record['evidence'][0]['start'] = -1
            elif mutation == 'fact-reference': record['facts'][0]['evidenceIds'] = ['missing']
            elif mutation == 'entity-reference': record['entities'][0]['factIds'] = ['missing']
            elif mutation == 'empty-facts': record['facts'] = []
            else: record['taskStatus']['article-summary'] = 'verified'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError): self.save(record)
            self.assertEqual(original, self.path.read_bytes())

    def test_capture_or_draft_cannot_be_saved_as_review(self):
        with self.assertRaises(ValueError): self.save(self.captures[0])
        record = self.review()
        record.pop('reviewedBy')
        with self.assertRaises(ValueError): self.save(record)

    def test_metadata_and_partial_cannot_be_body_ready(self):
        for status, basis in [('partial', 'partial'), ('metadata_only', 'metadata'), ('unavailable', 'none')]:
            self.captures[0]['contentStatus'] = status
            self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl', self.captures)
            record = self.review()
            with self.assertRaises(ValueError): self.save(record)
            record['basis'] = basis
            with self.assertRaises(ValueError): self.save(record)
            record['taskStatus'] = {task: 'held' for task in shared.TASKS}
            record['unresolved'] = ['全工程: 通常記事の本文確認ができない']
            record['facts'], record['entities'], record['evidence'] = [], [], []
            self.save(record)
            self.assertEqual(self.view()['sharedReview']['taskStatus'], 'held')
            self.assertNotIn('content', self.view())
            self.assertIn('content', self.view(include_body=True))

    def test_ready_summary_requires_body_evidence_not_just_title(self):
        record = self.review()
        record['evidence'][0].update(field='title', quote=self.articles[0]['article']['title'][:3])
        with self.assertRaisesRegex(ValueError, 'body evidence'): self.save(record)

    def test_corrupted_or_tampered_review_falls_back_to_body(self):
        self.save()
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        rows[0]['evidence'][0]['quote'] = 'wrong'
        self.write_jsonl(f'{shared.DIRECTORY}/{self.day}.jsonl', rows)
        self.assertEqual(self.view()['sharedReview']['status'], 'invalid')
        self.assertIn('content', self.view())
        self.path.write_text('{broken', encoding='utf-8')
        payload = reader.build_payload(self.root, self.day, 'article-summary')
        self.assertTrue(payload['warnings'])
        self.assertIn('content', payload['articles'][0])
        with self.assertRaises(ValueError): self.save()
        self.assertEqual(self.path.read_text(), '{broken')

    def test_check_only_and_bad_batch_are_atomic(self):
        self.assertEqual(writer.save_reviews(self.root, self.day, self.review(), True)['saved'], 0)
        self.assertFalse(self.path.exists())
        invalid = self.review()
        invalid['inputHash'] = 'outdated'
        with self.assertRaises(ValueError): self.save([self.review(), invalid])
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError): self.save([self.review(), self.review()])
        self.assertFalse(self.path.exists())

    def test_merge_updates_same_identity_and_preserves_other_records(self):
        self.save()
        updated = self.review()
        updated['facts'][0]['text'] = '再確認した事実'
        self.save(updated)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)
        self.captures[0]['contentText'] += '新しい本文'
        self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl', self.captures)
        self.save()
        self.assertEqual(len(self.path.read_text().splitlines()), 2)

    def test_write_lock_does_not_overwrite_or_remove_other_writer_lock(self):
        self.save()
        before = self.path.read_bytes()
        lock = self.path.with_suffix('.jsonl.lock')
        lock.write_text('other writer', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'write lock'): self.save()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(lock.read_text(), 'other writer')

    def copy_to_next_day(self):
        self.day = '2026-09-11'
        run, articles = copy.deepcopy(self.run), copy.deepcopy(self.articles)
        run['runDate'] = self.day
        for row in articles: row['runDate'] = self.day
        self.write_jsonl(f'content/structured-records/{self.day}.jsonl', [run, *articles])
        self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl', self.captures)

    def test_same_body_reused_across_dates_but_future_review_not_used(self):
        self.save()
        self.copy_to_next_day()
        self.assertEqual(self.view()['sharedReview']['record']['sourceDate'], '2026-09-10')
        self.assertTrue(self.view()['bodyOmitted'])
        future = self.root / shared.DIRECTORY / '2026-09-12.jsonl'
        self.path.rename(future)
        self.assertEqual(self.view()['sharedReview']['status'], 'missing')

    def test_non_body_hold_is_reconsidered_on_new_day(self):
        self.captures[0]['contentStatus'] = 'unavailable'
        self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl', self.captures)
        record = self.review()
        record.update(basis='none', taskStatus={t: 'held' for t in shared.TASKS}, facts=[], evidence=[], entities=[], unresolved=['全工程: 本文取得失敗'])
        self.save(record)
        self.copy_to_next_day()
        self.assertEqual(self.view()['sharedReview']['status'], 'stale')

    def test_policy_missing_does_not_reuse_or_save(self):
        self.save()
        (self.root / shared.RULES).unlink()
        self.assertIn('content', self.view())
        with self.assertRaisesRegex(ValueError, 'Missing shared'): self.save()

    def test_oversized_packet_rejected_not_truncated(self):
        record = self.review()
        record['unresolved'] = ['限界' * 800] * 20
        with self.assertRaisesRegex(ValueError, '30000'): self.save(record)

    def test_long_body_packet_is_smaller_and_all_articles_remain(self):
        self.captures[0]['contentText'] *= 1000
        self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl', self.captures)
        baseline = len(json.dumps(self.view(), ensure_ascii=False))
        self.save()
        reused = self.view()
        self.assertLess(len(json.dumps(reused, ensure_ascii=False)), baseline)
        payload = reader.build_payload(self.root, self.day, 'article-summary')
        self.assertEqual(payload['totalArticles'], 1)
        self.assertEqual(payload['returnedArticles'], 1)


if __name__ == '__main__':
    unittest.main()
