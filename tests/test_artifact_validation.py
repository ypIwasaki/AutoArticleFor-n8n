from __future__ import annotations
import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import autoarticle_artifacts as artifacts
import autoarticle_ops as ops
from autoarticle_progress import Progress, Blocked, write_json
import test_article_review_facts as fixtures


class ArtifactTests(unittest.TestCase):
    setUp = fixtures.SharedReviewTests.setUp
    write_jsonl = fixtures.SharedReviewTests.write_jsonl
    write_day = fixtures.SharedReviewTests.write_day
    review = fixtures.SharedReviewTests.review
    save = fixtures.SharedReviewTests.save

    def setup_artifacts(self):
        self.save()
        self.p = Progress(self.root, self.day, 'http://127.0.0.1:5678')
        self.url = self.articles[0]['article']['url']
        self.summary = f'## Source-by-source Notes\n\n1. [Title]({self.url})\n   - 本文確認: 確認済み\n   - 要約: Reviewed summary\n   - 根拠: [source]({self.url})\n'
        self.put('summary', self.summary)
        self.talent = {'proposalVersion':1, 'proposalDate':self.day, 'articles':[
            {'article_key':'a', 'url':self.url, 'title':'Title', 'excerpt':'', 'source':'', 'published_at':'', 'last_seen_at':self.day+'T00:00:00Z'}],
            'talents':[{'talent_id':'t', 'display_name':'Name', 'organization':'', 'aliases_json':'["Name"]', 'status':'pending', 'search_enabled':False, 'auto_discovered':False, 'last_seen_at':self.day+'T00:00:00Z'}],
            'articleTalents':[{'relation_key':'r', 'article_key':'a', 'talent_id':'t', 'matched_aliases_json':'["Name"]', 'matched_fields':'content', 'evidence_text':'Reviewed evidence', 'confidence':0.9, 'detection_method':'ai_review', 'last_seen_at':self.day+'T00:00:00Z'}]}
        self.put('talent-review', self.talent)
        self.classification = {'proposalVersion':1,'proposalDate':self.day,'classifications':[
            {'article_url':self.url, 'article_type':'news_article','primary_category':'other', 'secondary_categories_json':[], 'relevance':'in_scope','confidence':0.9,'evidence_text':'Evidence','classification_method':'ai_review','classified_at':self.day+'T00:00:00Z'}]}
        self.put('classification-review', self.classification)
        taxonomy = Path(__file__).resolve().parents[1] / 'config/article-classification-taxonomy.json'
        write_json(self.root / 'config/article-classification-taxonomy.json', json.loads(taxonomy.read_text()))

    def put(self, step, value):
        path = self.p.path(self.p.outputs(step)[0])
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(value if isinstance(value,str) else json.dumps(value), encoding='utf-8')

    def invalid(self, step, code):
        with self.assertRaises(artifacts.ArtifactInvalid) as caught:
            artifacts.validate(self.p, step)
        self.assertIn(code, [e['code'] for e in caught.exception.result['errors']])
        return caught.exception.result

    def test_real_review_and_parser_counts_read_only(self):
        self.setup_artifacts()
        before = {str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for step in ('summary','talent-review','classification-review'):
            self.assertEqual(artifacts.validate(self.p,step)['status'],'valid')
        self.assertEqual(artifacts.validate(self.p,'summary')['counts']['dashboardSummaries'],1)
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_old_heading_and_missing_summary_marker_rejected(self):
        self.setup_artifacts()
        for value in (self.summary.replace('1. [','### 1. ['), self.summary.replace('- 要約:', '- 本文:')):
            self.put('summary',value)
            self.invalid('summary','summary_ready_coverage_mismatch')

    def test_missing_confirmation_and_duplicate_rejected(self):
        self.setup_artifacts()
        self.put('summary',self.summary.replace('確認済み','未確認'))
        self.invalid('summary','summary_not_readable_by_dashboard')
        self.put('summary',self.summary + self.summary.split('\n\n')[1])
        self.invalid('summary','summary_duplicate_url')

    def test_stale_or_held_review_never_promoted(self):
        self.setup_artifacts()
        record = self.review();record['taskStatus']['article-summary']='held';record['unresolved']=['held']
        self.save(record)
        self.invalid('summary','summary_review_not_current_ready')
        self.save()
        self.captures[0]['contentText'] += 'changed'
        self.write_jsonl(f'content/article-body-captures/{self.day}.jsonl',self.captures)
        self.invalid('classification-review','classification_review_not_current_ready')

    def test_required_fields_duplicate_and_reference(self):
        self.setup_artifacts()
        for mutation,code in [('field','proposal_stored_fields_must_be_explicit'),('duplicate','duplicate_proposal_keys'),('reference','existing_references_require_db_check')]:
            value = copy.deepcopy(self.talent)
            if mutation=='field': del value['articleTalents'][0]['confidence']
            elif mutation=='duplicate': value['articles'].append(value['articles'][0])
            else: value['articleTalents'][0]['talent_id']='absent'
            self.put('talent-review',value)
            self.invalid('talent-review',code)

    def test_existing_db_reference_allowed_and_missing_rejected(self):
        self.setup_artifacts()
        saved = copy.deepcopy(self.talent)
        self.talent['talents']=[];self.put('talent-review',self.talent)
        result = artifacts.validate(self.p,'talent-review',existing=lambda:saved)
        self.assertEqual(result['status'],'valid')
        with self.assertRaises(artifacts.ArtifactInvalid) as caught:
            artifacts.validate(self.p,'talent-review',existing=lambda:{})
        self.assertIn('relation_reference_missing',[e['code'] for e in caught.exception.result['errors']])

    def test_taxonomy_and_timestamp_rejected(self):
        self.setup_artifacts()
        self.classification['classifications'][0]['primary_category']='unknown'
        self.classification['classifications'][0]['classified_at']='2026-09-10'
        self.put('classification-review',self.classification)
        result=self.invalid('classification-review','classification_primary_category_invalid')
        self.assertIn('classification_timestamp_invalid',[e['code'] for e in result['errors']])

    def test_empty_classifications_and_all_held_summary_allowed(self):
        self.setup_artifacts()
        self.classification['classifications']=[];self.put('classification-review',self.classification)
        self.assertEqual(artifacts.validate(self.p,'classification-review')['counts']['classifications'],0)
        record=self.review();record['taskStatus']={t:'held' for t in record['taskStatus']};record['unresolved']=['held'];self.save(record)
        self.put('summary','## Source-by-source Notes\n\n全件保留。要約なし。\n')
        self.assertEqual(artifacts.validate(self.p,'summary')['counts']['dashboardSummaries'],0)

    def test_weekly_date_and_numeric_validation(self):
        self.setup_artifacts()
        self.put('weekly','---\nweekStart: 2026-09-07\nweekEnd: 2026-09-13\ncoveredThrough: 2026-09-09\n---\n')
        self.invalid('weekly','weekly_cutoff_mismatch')
        self.put('weekly','---\nweekStart: 2026-09-07\nweekEnd: 2026-09-13\ncoveredThrough: 2026-09-10\n---\n')
        operator=ops.Operations(self.root,self.day)
        with patch.object(operator,'check_weekly',side_effect=Blocked('weekly_validation_failed')) as numeric:
            with self.assertRaisesRegex(Blocked,'weekly_validation_failed'): operator.validate_artifact('weekly')
            numeric.assert_called_once()

    def test_invalid_checkpoint_preserves_previous_state(self):
        self.setup_artifacts()
        operator=ops.Operations(self.root,self.day)
        operator.checkpoint('summary',[self.p.outputs('summary')[0]],'Reviewed')
        before=operator.progress.file.read_bytes()
        self.put('summary',self.summary.replace('1. [','### 1. ['))
        with self.assertRaises(artifacts.ArtifactInvalid): operator.checkpoint('summary',[self.p.outputs('summary')[0]],'Reviewed')
        self.assertEqual(before,operator.progress.file.read_bytes())
        self.assertFalse(operator.progress.current(operator.progress.load()['steps']['summary']))

    def test_dashboard_uses_same_parser(self):
        import importlib.util
        self.setup_artifacts()
        path = Path(__file__).resolve().parents[1] / 'apps/talent-dashboard/server.py'
        spec = importlib.util.spec_from_file_location('artifact_dashboard_test', path)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        with patch.object(server, 'PROJECT_ROOT', self.root):
            entries = server.load_article_summaries()
        checked = artifacts.validate(self.p, 'summary')
        self.assertEqual(len(entries), checked['counts']['dashboardSummaries'])
        self.assertEqual(entries[self.url]['text'], 'Reviewed summary')

    def test_error_samples_bounded_with_total_retained(self):
        self.setup_artifacts()
        self.put('summary', self.summary + self.summary.split('\n\n')[1] * 20)
        result = self.invalid('summary', 'summary_duplicate_url')
        self.assertEqual(result['errorCount'], 20)
        self.assertEqual(len(result['errors']), 10)
        self.assertNotIn(self.url, json.dumps(result))

    def test_cli_short_errors_no_state_or_article_text(self):
        self.setup_artifacts()
        self.put('summary',self.summary.replace('1. [','### 1. ['))
        output=io.StringIO()
        with patch.object(ops,'ROOT',self.root), contextlib.redirect_stdout(output):
            code=ops.main(['--date',self.day,'validate','summary'])
        self.assertEqual(code,2)
        self.assertNotIn(self.url,output.getvalue())
        self.assertFalse(self.p.file.exists())
        self.assertLessEqual(len(json.loads(output.getvalue())['artifactValidation']['errors']),10)

if __name__=='__main__': unittest.main()
