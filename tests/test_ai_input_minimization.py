"""The six agreed representative patterns; synthetic data only."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import ai_minimization_fixture as fx
import ai_input_minimization as mini
import read_ai_inputs as inputs
import project_database as db
import project_readers as project
import project_business_writes as business
import article_review_facts as shared
import capture_article_contents as capture
from autoarticle_progress import Progress
OUT=db.ROOT/'.operation-state/ai-input-minimization'
class Patterns(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ai-input-',dir=OUT)
        self.root=fx.setup(Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)
        self.path=project.path_for(self.root)
    def payload(self,task):return inputs.build_payload(self.root,fx.DAY,task,limit=1)
    def ref(self,task):return self.payload(task)['articles'][0]['ref']
    def submit(self,op,kind,payload,**kw):return business.submit(op,kind,payload,self.path,self.root,**kw)
    def scalar(self,sql):
        with project.reader(self.root) as r:return r.c.execute(sql).fetchone()[0]
    def save_summary(self):
        self.submit('fixture-save-summary','summary',dict(day=fx.DAY,summaries=[dict(ref=self.ref('article-summary'),text='星野アキが9月20日に東京の音楽イベントに出演。参加費は3000円。')]))
    def proposal(self,task,result='confirmed'):
        ref=self.ref(task)
        if task=='talent-index':
            with project.reader(self.root) as r:mapping,_,_,_=mini.resolve(r.c,ref,fx.DAY,task)
            key=db.checksum(mapping['articleId'].encode())
            return dict(proposalVersion=1,proposalDate=fx.DAY,
                reviewedArticles=[dict(ref=ref,result=result)],
                articles=[dict(ref=ref,last_seen_at=fx.DAY+'T01:10:00Z')],
                talents=[dict(talent_id='hoshino-aki',display_name='星野アキ',organization='青空社',aliases_json='["星野アキ"]',status='pending',search_enabled=False,auto_discovered=True,last_seen_at=fx.DAY+'T01:10:00Z')],
                articleTalents=[dict(articleRef=ref,talent_id='hoshino-aki',matched_aliases_json='["星野アキ"]',matched_fields='content',evidence_text=fx.BODY.split('背景')[0],confidence=0.95,detection_method='ai_review',last_seen_at=fx.DAY+'T01:10:00Z')])
        return dict(proposalVersion=1,proposalDate=fx.DAY,reviewedArticles=[dict(ref=ref,result=result)],
           classifications=[dict(ref=ref,article_type='news_article',primary_category='other',secondary_categories_json=[],relevance='in_scope',confidence=0.9,evidence_text=fx.BODY.split('背景')[0],classification_method='ai_review',classified_at=fx.DAY+'T01:10:00Z')])
    def change_body(self,suffix='別の本文版を保存した。',status='verified'):
        body=fx.BODY+suffix if status=='verified' else ''
        record=dict(recordType='article-content',originalUrl=fx.URL,resolvedUrl=fx.URL,contentStatus=status,contentText=body,contentMarkdown=body,contentLength=len(body),fetchedAt=fx.DAY+'T02:00:00Z',contentCompleteness='full' if body else '',contentType='article',failureReason='' if body else 'HTTP 404')
        self.submit('fixture-change-'+shared.digest(record),'capture',dict(day=fx.DAY,record=record))
    def test_1_new_article(self):
        initial=self.payload('article-summary')
        self.assertEqual(initial['articles'][0]['state'],'needs_review')
        self.assertTrue(initial['articles'][0]['content']['text'])
        authored=fx.review(self.root)
        for k in ('url','inputHash','policyHash','reviewVersion'):authored.pop(k)
        authored['ref']=initial['articles'][0]['ref']
        self.submit('fixture-reviewed','reviews',dict(day=fx.DAY,records=[authored]))
        self.save_summary()
        talent=self.proposal('talent-index')
        self.submit('fixture-talent-proposal','proposal',dict(day=fx.DAY,directory='talent-index-proposals',document=talent))
        with project.reader(self.root) as r:saved=list(r.documents('talent-index-proposals'))[0][1]
        self.submit('fixture-talent-apply','talent',dict(proposal=saved))
        classification=self.proposal('article-classification')
        self.submit('fixture-classification-proposal','proposal',dict(day=fx.DAY,directory='article-classification-proposals',document=classification))
        with project.reader(self.root) as r:
            saved=list(r.documents('article-classification-proposals'))[0][1]
            self.assertEqual(saved['classifications'][0]['article_url'],fx.URL)
        self.submit('fixture-classification-apply','classification',dict(proposal=saved))
        for task in shared.TASKS:self.assertEqual(self.payload(task)['excluded'],{'saved':1})
        with project.reader(self.root) as r:
            self.assertIn('3000円',r.summaries()[fx.URL]['text'])
            self.assertEqual(len(r.table('articles')),1)
        # Keep only this isolated DB/root for the single browser display check.
        import shutil
        target=OUT/'display-fixture'
        if target.exists():
            with project.reader(target) as prior,project.reader(self.root) as current:
                self.assertEqual(prior.summaries(),current.summaries())
                self.assertEqual(len(prior.table('articles')),len(current.table('articles')))
            return  # UI unchanged: retain the already inspected representative display.

        target.mkdir()
        with db.connect(self.path,readonly=True) as source,db.connect(target/'data/autoarticle.sqlite') as dest:source.backup(dest)
        for directory in ('docs','config','content'):
            if (self.root/directory).exists():shutil.copytree(self.root/directory,target/directory)
    def test_2_partial_saved(self):
        fx.save_review(self.root);self.save_summary()
        self.assertEqual(self.payload('article-summary')['returnedArticles'],0)
        self.assertEqual(self.payload('talent-index')['returnedArticles'],1)
        # Persist a confirmed negative finding: explicit article scope, not an empty file.
        ref=self.ref('talent-index')
        negative=dict(proposalVersion=1,proposalDate=fx.DAY,articles=[],talents=[],articleTalents=[],reviewedArticles=[dict(ref=ref,result='none')])
        self.submit('fixture-negative','proposal',dict(day=fx.DAY,directory='talent-index-proposals',document=negative))
        saved_before=self.scalar("SELECT raw_json FROM legacy_history_records WHERE kind='ai-stage-completion' LIMIT 1")
        self.change_body()
        rule=self.root/shared.RULES;rule.write_text(rule.read_text()+'\nルールの更新\n')
        for task in ('article-summary','talent-index'):
            self.assertEqual(self.payload(task)['excluded'],{'saved':1})
        self.assertEqual(self.payload('article-classification')['returnedArticles'],1)
        self.assertEqual(saved_before,self.scalar("SELECT raw_json FROM legacy_history_records WHERE kind='ai-stage-completion' LIMIT 1"))
        with project.reader(self.root) as r:
            self.assertEqual(list(r.documents('talent-index-proposals'))[0][1]['reviewedArticles'][0]['result'],'none')
        # Resume dependency check ignores input/rule changes for saved results.
        p=Progress(self.root,fx.DAY,'local')
        entry=dict(status='completed',target='local',dependencyVersion=1,dependencyStep='summary',files=p.fingerprints(p.inputs('summary')+p.outputs('summary')))
        rule.write_text(rule.read_text()+'\n追加変更\n')
        self.assertTrue(p.changes(entry)['current'])
        # Reuse old artifacts after changes without requiring current ready inputs.
        self.submit('fixture-preserve-summary','summary',dict(day=fx.DAY,summaries=[]))
        self.submit('fixture-preserve-negative','proposal',dict(day=fx.DAY,directory='talent-index-proposals',document=negative))
        self.assertEqual(self.payload('talent-index')['excluded'],{'saved':1})
        self.assertEqual(self.payload('article-summary')['excluded'],{'saved':1})
    def test_3_unsaved(self):
        fx.save_review(self.root)
        for task in shared.TASKS:self.assertEqual(self.payload(task)['returnedArticles'],1)
        empty=dict(proposalVersion=1,proposalDate=fx.DAY,articles=[],talents=[],articleTalents=[])
        self.submit('fixture-draft','proposal',dict(day=fx.DAY,directory='talent-index-proposals',document=empty))
        self.assertEqual(self.payload('talent-index')['returnedArticles'],1)
        ref=self.ref('article-summary')
        def fail(stage,index):
            if stage=='business':raise RuntimeError('synthetic save failure')
        with self.assertRaises(RuntimeError):
            self.submit('fixture-failed-save','summary',dict(day=fx.DAY,summaries=[dict(ref=ref,text='未保存の要約')]),fault=fail)
        self.assertEqual(self.scalar('SELECT count(*) FROM article_summaries'),0)
        self.assertEqual(self.payload('article-summary')['returnedArticles'],1)
        with self.assertRaises(ValueError):
            self.submit('fixture-unreviewed-draft','summary',dict(day=fx.DAY,text='## Source-by-source Notes\n- [draft]('+fx.URL+') - 本文確認: 未確認 - 要約: draft'))
        self.assertEqual(self.payload('article-summary')['returnedArticles'],1)
    def test_4_unavailable(self):
        self.change_body(status='unavailable')
        entry=dict(status='unavailable',reason='HTTP 404',retry_after=1)
        original=copy.deepcopy(entry)
        for refresh,retry in ((False,False),(False,True),(True,True)):
            self.assertFalse(capture.eligible_article(entry,refresh,retry))
        self.assertEqual(entry,original)
        for _ in range(2):
            for task in ('article-summary','article-classification'):
                self.assertEqual(self.payload(task)['excluded'],{'unavailable':1})
            self.assertEqual(self.payload('talent-index')['returnedArticles'],1)
        self.assertEqual(self.scalar('SELECT count(*) FROM content_fetch_attempts'),2)
        self.assertEqual(self.scalar("SELECT failure_reason FROM content_fetch_attempts WHERE status='unavailable'"),'HTTP 404')
    def test_5_held(self):
        record=fx.review(self.root)
        record['taskStatus']['talent-index']='held';record['unresolved']=['talent-index: 契約期間が不足']
        record['holds']={'talent-index':dict(reason='契約期間が不足',missingTopics=['contract:星野アキ'])}
        fx.save_review(self.root,record)
        self.assertEqual(self.payload('talent-index')['excluded'],{'held':1})
        unrelated=fx.review(self.root)
        unrelated['facts'][0]['topics']=['event']
        fx.save_review(self.root,unrelated,'fixture-unrelated-review')
        self.assertEqual(self.payload('talent-index')['excluded'],{'held':1})
        rule=self.root/shared.RULES;rule.write_text(rule.read_text()+'\n管理用更新\n')
        self.assertEqual(self.payload('talent-index')['excluded'],{'held':1})
        relevant=fx.review(self.root)
        quote='追加根拠：出演契約の期間は1年間である。';start=fx.BODY.index(quote)
        relevant['facts'].append(dict(id='f2',text=quote,evidenceIds=['e2'],topics=['contract:星野アキ']))
        relevant['evidence'].append(dict(id='e2',field='contentText',start=start,end=start+len(quote),quote=quote))
        relevant['entities'][0]['factIds'].append('f2')
        fx.save_review(self.root,relevant,'fixture-related-review')
        self.assertEqual(self.payload('talent-index')['articles'][0]['resume']['state'],'resumed')
        relevant['taskStatus']['talent-index']='held';relevant['unresolved']=['talent-index: 契約開始日の根拠が不足']
        relevant['holds']={'talent-index':dict(reason='契約開始日の根拠が不足',missingTopics=['contract:星野アキ'])}
        fx.save_review(self.root,relevant,'fixture-reheld-review')
        self.assertEqual(self.payload('talent-index')['excluded'],{'held':1})
        self.assertEqual(self.scalar('SELECT count(*) FROM review_records'),4)
        self.assertEqual(self.scalar("SELECT count(*) FROM review_input_snapshots"),4)
    def test_6_minimal_and_references(self):
        fx.save_review(self.root)
        ref=self.ref('talent-index')
        packet=self.payload('talent-index')['articles'][0]
        self.assertNotIn('url',packet);self.assertNotIn('databaseReferences',packet);self.assertNotIn('content',packet)
        self.assertEqual(packet['entities'][0]['name'],'星野アキ')
        self.assertEqual(mini.additional(self.root,fx.DAY,'talent-index',ref,'detail')['facts'],packet['facts'])
        tail=mini.additional(self.root,fx.DAY,'talent-index',ref,'body',6000,1000)
        self.assertEqual(tail['content']['text'],fx.BODY[6000:7000])
        evidence=mini.additional(self.root,fx.DAY,'talent-index',ref,'evidence',ids=['e1'])
        self.assertEqual(evidence['evidence'][0]['quote'],fx.BODY.split('背景')[0])
        self.assertEqual(mini.additional(self.root,fx.DAY,'talent-index',ref,'source')['url'],fx.URL)
        with self.assertRaises(ValueError):mini.additional(self.root,'2026-09-18','talent-index',ref)
        with self.assertRaises(ValueError):mini.additional(self.root,fx.DAY,'article-summary',ref)
        scoped=fx.review(self.root)
        quote='追加根拠：出演契約の期間は1年間である。';start=fx.BODY.index(quote)
        scoped['facts'].append(dict(id='f2',text=quote,evidenceIds=['e2']))
        scoped['evidence'].append(dict(id='e2',field='contentText',start=start,end=start+len(quote),quote=quote))
        scoped['taskFacts']={task:['f1'] for task in shared.TASKS}
        fx.save_review(self.root,scoped,'fixture-scoped')
        new_ref=self.ref('talent-index')
        self.assertEqual([f['id'] for f in self.payload('talent-index')['articles'][0]['facts']],['f1'])
        self.assertEqual(mini.additional(self.root,fx.DAY,'talent-index',new_ref,'facts',ids=['f2'])['facts'][0]['id'],'f2')
        self.change_body('変更された末尾')
        self.assertEqual(mini.additional(self.root,fx.DAY,'talent-index',ref,'body',6000,1000)['content']['text'],fx.BODY[6000:7000])
        # A stale frozen reference is useful for inspection but cannot approve a new artifact.
        with self.assertRaises(ValueError):
            self.submit('fixture-stale-proposal','proposal',dict(day=fx.DAY,directory='talent-index-proposals',
                document=dict(proposalVersion=1,proposalDate=fx.DAY,articles=[],talents=[],articleTalents=[],reviewedArticles=[dict(ref=ref,result='none')])))

if __name__=='__main__':unittest.main()
