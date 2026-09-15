"""Phase 5 tests run only in temporary databases."""
import contextlib
import copy
import json
import tempfile
from pathlib import Path
import unittest
import project_database as db
import continuous_database_sync as sync
import legacy_sync_projection as projection
import test_import_legacy_database as fixtures
import import_legacy_database as imp

class ContinuousSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'project.sqlite';db.migrate(self.path)
    def tearDown(self):self.temp.cleanup()
    def request(self,title='One'):
        row={'id':'article-one','title':title,'url':'https://example.test/one','excerpt':'','source':'fixture',
             'published_at':None,'created_at':'2026-09-15T00:00:00Z','updated_at':'2026-09-15T00:00:00Z','identity_state':'identified'}
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:before=sync.read_row(c,'article',{'id':row['id']})
        raw={'article_key':'original-key','title':title};source={'path':'n8n:fixture','position':'1','old_key':'original-key','raw':raw,'input_hash':sync.digest(raw)}
        return {'version':sync.VERSION,'receipt':{'status':'complete','completed_at':'2026-09-15T00:00:00Z'},
                'changes':[{'entity':'article','key':{'id':row['id']},'before':before,'after':row,'source':source}]}
    def send(self,operation,request,**kwargs):
        return sync.synchronize(operation,request,self.path,lambda r:None,projection.compare_scope,**kwargs)
    def counts(self):
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:return imp.counts(c)
    def test_insert_update_replay_and_full_backup(self):
        request=self.request();result=self.send('operation-first',request);before=self.counts()
        self.assertEqual(result,self.send('operation-first',request));self.assertEqual(before,self.counts())
        updated=self.request('Changed');self.assertEqual(self.send('operation-update',updated)['updated'],1)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            history=json.loads(c.execute("SELECT old_json FROM sync_row_history WHERE operation_id='operation-update'").fetchone()[0]);self.assertEqual(history['title'],'One')
        db.backup(self.root/'backup.sqlite',self.path);db.restore_check(self.root/'backup.sqlite',self.root/'restored.sqlite')
        self.assertEqual(sync.operation_status('operation-update',self.path),sync.operation_status('operation-update',self.root/'restored.sqlite'))
    def test_reused_id_with_different_content_is_audited_and_stops(self):
        request=self.request();self.send('operation-first',request);different=copy.deepcopy(request);different['changes'][0]['after']['title']='Different'
        with self.assertRaisesRegex(sync.SyncStopped,'operation_id_content_conflict'):self.send('operation-first',different)
        self.assertEqual(sync.operation_status('operation-first',self.path)['outcome'],'conflict')
        with self.assertRaisesRegex(sync.SyncStopped,'unresolved_operation_id_conflict'):self.send('operation-first',request)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:self.assertEqual(c.execute('SELECT title FROM articles').fetchone()[0],'One')
    def test_mid_transaction_failure_rolls_back_and_same_id_resumes(self):
        request=self.request()
        def fail(index):raise RuntimeError('intentional-test-failure')
        with self.assertRaises(RuntimeError):self.send('operation-first',request,fault=fail)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM articles').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM sync_row_history').fetchone()[0],0)
        self.assertEqual(sync.operation_status('operation-first',self.path)['outcome'],'failure')
        self.assertEqual(self.send('operation-first',request)['added'],1)
    def test_comparison_difference_and_input_change_roll_back(self):
        request=self.request()
        with self.assertRaisesRegex(sync.SyncStopped,'post_sync_comparison_difference'):
            sync.synchronize('operation-first',request,self.path,lambda r:None,lambda c,r:[{'difference':True}])
        self.assertEqual(self.counts()['articles'],0)
        self.assertEqual(sync.operation_status('operation-first',self.path)['outcome'],'difference')
        def changed(receipt):raise sync.SyncStopped('live_input_changed')
        with self.assertRaisesRegex(sync.SyncStopped,'live_input_changed'):
            sync.synchronize('operation-first',request,self.path,changed,projection.compare_scope)
        self.assertEqual(self.counts()['articles'],0)
    def test_unfinished_legacy_rejected_and_target_precondition(self):
        request=self.request();request['receipt']['status']='running'
        with self.assertRaises(ValueError):self.send('operation-first',request)
        self.assertEqual(self.counts()['sync_runs'],0)
        request=self.request();self.send('operation-first',request)
        with self.assertRaisesRegex(sync.SyncStopped,'target_changed_before_sync'):self.send('operation-other',request)
    def test_held_state_cannot_be_promoted(self):
        request=self.request();request['changes'][0]['after']['identity_state']='held';self.send('operation-first',request)
        request=self.request('Changed')
        with self.assertRaisesRegex(sync.SyncStopped,'held_or_unverified'):self.send('operation-update',request)
    def test_real_import_projection_add_and_replay(self):
        h=fixtures.LegacyImportTests();h.setUp()
        try:
            snapshot=h.fixture();imp.import_snapshot(snapshot,h.path)
            receipt={'status':'complete','completed_at':'2026-09-15T00:00:00Z','snapshot':str(snapshot),'snapshot_hash':'fixture'}
            request=projection.plan(h.path,self.path,receipt)
            result=self.send('operation-import',request);self.assertGreater(result['added'],0)
            before=self.counts();self.assertEqual(result,self.send('operation-import',request));self.assertEqual(before,self.counts())
            self.assertEqual(projection.plan(h.path,self.path,receipt)['changes'],[])
        finally:h.tearDown()

class AllBusinessScopeTests(ContinuousSyncTests):
    def complete_request(self):
        stamp='2026-09-15T00:00:00Z';article=self.request()['changes'][0]['after'];raw_body={'content_text':'Saved body','content_hash':db.checksum(b'Saved body'),'body_length':10,'content_status':'verified'}
        data={
          'article':[article],
          'identifier':[dict(id='identifier-one',article_id=article['id'],source='fixture',kind='legacy_key',value='legacy-one',match_state='exact')],
          'collection':[dict(id='collection-one',source='fixture',source_record='one',run_date='2026-09-15',workflow_execution_id='1',search_conditions_json='{}',observed_at=stamp)],
          'occurrence':[dict(id='occurrence-one',collection_run_id='collection-one',article_id=article['id'],source_record='one',title='One',excerpt='',url=article['url'],published_at=None,observations_json='{}')],
          'body':[dict(id='body-one',payload_hash='a'*64,text='Saved body',text_hash=db.checksum(b'Saved body'),markdown='',metadata_json='{}',non_content_text='',extraction_scope='',raw_json='{}')],
          'bodyVersion':[dict(id='version-one',article_id=article['id'],payload_id='body-one',first_observed_at=stamp,last_observed_at=stamp)],
          'fetchAttempt':[dict(id='fetch-one',article_id=article['id'],version_id='version-one',fetched_at=stamp,status='verified',completeness=None,original_url=article['url'],resolved_url=None,failure_reason=None,extraction_method=None,retry_after=None,raw_json=db.canonical(raw_body),body_integrity='consistent',source_status='verified',stored_body_hash=raw_body['content_hash'],stored_body_length=10,source_content_path=None,computed_body_hash=raw_body['content_hash'])],
          'review':[dict(id='review-one',article_id=article['id'],content_version_id='version-one',input_hash='input',rule_hash='rule',basis='body',reviewer='fixture',reviewed_at=stamp,status='current',raw_json='{}')],
          'taskStatus':[dict(review_id='review-one',task='article-summary',status='ready')],
          'fact':[dict(id='fact-one',review_id='review-one',fact='Fact',raw_json='{}')],
          'entity':[dict(id='entity-one',review_id='review-one',name='Name',kind='person',raw_json='{}')],
          'evidence':[dict(id='evidence-one',review_id='review-one',input_field='contentText',start_offset=0,end_offset=5,quote='Saved')],
          'factEvidence':[dict(fact_id='fact-one',evidence_id='evidence-one')],
          'entityEvidence':[dict(entity_id='entity-one',evidence_id='evidence-one')],
          'entityFact':[dict(entity_id='entity-one',fact_id='fact-one')],
          'summary':[dict(id='summary-one',article_id=article['id'],review_id='review-one',summary='Summary',saved_at=stamp,version=1,is_current=1,source='fixture',raw_json='{}')],
          'talent':[dict(id='talent-one',display_name='Name',organization='Organization',status='pending',search_enabled=0,auto_discovered=1,last_seen_at=stamp,raw_json='{}')],
          'alias':[dict(talent_id='talent-one',alias='Alias')],
          'relationship':[dict(id='relationship-one',article_id=article['id'],talent_id='talent-one',review_id='review-one',evidence='Saved',confidence=1.0,state='current',raw_json='{}')],
          'classification':[dict(id='classification-one',article_id=article['id'],review_id='review-one',article_type='news',primary_category='test',relevance='in_scope',confidence=1.0,evidence='Saved',classified_at=stamp,version=1,is_current=1,raw_json='{}')],
          'secondaryCategory':[dict(classification_id='classification-one',category='secondary')],
          'feedback':[dict(id='feedback-one',article_id=article['id'],is_rejected=0,reason_code='',reviewed_at=stamp,source='fixture',version=1,is_current=1,raw_json='{}')]}
        body=data['body'][0];body['payload_hash']=sync.digest({k:v for k,v in body.items() if k not in ('id','payload_hash','text_hash')})
        request=self.request();request['changes']=[]
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            for entity in projection.ORDER:
                for row in data.get(entity,[]):
                    source_raw=raw_body if entity=='fetchAttempt' else row
                    source={'path':'fixture:'+entity,'position':'1','old_key':'legacy-one','raw':source_raw,'input_hash':sync.digest(source_raw)}
                    key={k:row[k] for k in sync.primary_key(c,sync.ENTITIES[entity])}
                    request['changes'].append(dict(entity=entity,key=key,before=None,after=row,source=source))
        return request
    def test_all_business_categories_added_and_mutable_updates_audited(self):
        request=self.complete_request();result=self.send('operation-all',request);self.assertEqual(result['added'],22)
        before=self.counts();self.assertEqual(result,self.send('operation-all',request));self.assertEqual(before,self.counts())
        update=copy.deepcopy(request);update['changes']=[]
        fields={'article':('title','New title'),'collection':('search_conditions_json','{"updated":true}'),'occurrence':('excerpt','New excerpt'),'bodyVersion':('last_observed_at','2026-09-16T00:00:00Z'),'summary':('summary','Revised summary'),'talent':('organization','Updated organization'),'relationship':('evidence','Updated evidence'),'classification':('evidence','Updated classification evidence'),'feedback':('reason_code','Updated reason')}
        for change in request['changes']:
            if change['entity'] in fields:
                new=copy.deepcopy(change);new['before']=copy.deepcopy(new['after']);field,value=fields[new['entity']];new['after'][field]=value;new['source']['raw']=new['after'];new['source']['input_hash']=sync.digest(new['after']);update['changes'].append(new)
        self.assertEqual(self.send('operation-updates',update)['updated'],len(fields))
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertFalse(c.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(c.execute("SELECT count(*) FROM sync_row_history WHERE operation_id='operation-updates' AND old_json IS NOT NULL").fetchone()[0],len(fields))
    def test_missing_body_cannot_receive_payload_reference(self):
        request=self.complete_request();fetch=next(x for x in request['changes'] if x['entity']=='fetchAttempt');fetch['source']['raw']['content_text']='';fetch['source']['input_hash']=sync.digest(fetch['source']['raw']);fetch['after']['body_integrity']='held_missing_body'
        with self.assertRaisesRegex(sync.SyncStopped,'missing_body_requires_null_version'):self.send('operation-all',request)
        self.assertEqual(self.counts()['articles'],0)

if __name__=='__main__':unittest.main()
