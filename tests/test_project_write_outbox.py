import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project_database as db
import project_write_outbox as writes
import project_business_writes as business


class ProjectWrites(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='phase7-test-',dir=os.environ.get('AUTOARTICLE_TEST_TMPDIR'))
        self.root=Path(self.temp.name);self.path=self.root/'project.sqlite'
        db.migrate(self.path);writes.set_write_target('n8n-daily','project-db',self.path)
        self.records=[{'recordType':'run','runDate':'2026-09-17','generatedAt':'2026-09-17T01:00:00Z','capturedArticleCount':1,'workflowExecutionId':'ordinary-1'},
            {'recordType':'article','runDate':'2026-09-17','articleIndex':1,'article':{'url':'https://example.test/a','title':'Example','excerpt':'saved','publishedAt':'2026-09-17T00:00:00Z','source':'test'}}]
        self.operation='phase7-test-collection-1'
        self.file=self.root/'content/structured-records/2026-09-17.jsonl'
    def tearDown(self): self.temp.cleanup()
    def scalar(self,sql):
        with sqlite3.connect(str(self.path)) as c:return c.execute(sql).fetchone()[0]
    def submit(self,records=None,fault=None):
        return business.submit_collection(self.operation,records or self.records,self.path,self.root,fault)
    def test_new_and_replay(self):
        first=self.submit();self.assertEqual('complete',first['compatibility'])
        self.assertEqual(self.records,[json.loads(x) for x in self.file.read_text().splitlines()])
        before=self.scalar('SELECT count(*) FROM source_records');again=self.submit()
        self.assertEqual(first,again);self.assertEqual(before,self.scalar('SELECT count(*) FROM source_records'))
        self.assertEqual(1,self.scalar('SELECT count(*) FROM articles'))
    def test_id_conflict(self):
        self.submit();other=copy.deepcopy(self.records);other[1]['article']['title']='Different'
        with self.assertRaises(db.Conflict):self.submit(other)
        self.assertEqual(1,self.scalar('SELECT count(*) FROM articles'))
    def test_business_rollback_and_same_id_resume(self):
        def fault(stage,index):
            if stage=='business' and index==3:raise RuntimeError('test failure')
        with self.assertRaises(RuntimeError):self.submit(fault=fault)
        self.assertEqual(0,self.scalar('SELECT count(*) FROM collection_runs'))
        self.assertEqual(0,self.scalar('SELECT count(*) FROM project_write_requests'))
        self.assertEqual(0,self.scalar('SELECT count(*) FROM sync_row_history'))
        self.assertFalse(self.file.exists());self.submit()
        self.assertEqual(1,self.scalar('SELECT count(*) FROM collection_runs'))
    def test_db_only_data_is_delivered_before_rollback(self):
        def fault(stage,index):
            if stage=='after_database_commit':raise RuntimeError('lost result')
        with self.assertRaises(RuntimeError):self.submit(fault=fault)
        self.assertFalse(self.file.exists());self.assertEqual(1,self.scalar('SELECT count(*) FROM article_occurrences'))
        with self.assertRaises(writes.WriteStopped):writes.set_write_target('n8n-daily','legacy',self.path)
        state=writes.status(self.operation,self.path);self.assertTrue(state['dbCommitted']);self.assertEqual(1,state['pendingDeliveries'])
        self.submit();writes.set_write_target('n8n-daily','legacy',self.path)
        self.assertEqual(self.records,[json.loads(x) for x in self.file.read_text().splitlines()])
    def test_lost_delivery_ack_replay(self):
        def fault(stage,index):
            if stage=='after_delivery':raise RuntimeError('lost ack')
        with self.assertRaises(RuntimeError):self.submit(fault=fault)
        self.assertTrue(self.file.exists());before=self.file.read_bytes();self.submit()
        self.assertEqual(before,self.file.read_bytes());self.assertEqual(1,self.scalar('SELECT count(*) FROM article_occurrences'))
    def test_changed_compatibility_file_is_not_overwritten(self):
        def fault(stage,index):
            if stage=='after_database_commit':raise RuntimeError('pause')
        with self.assertRaises(RuntimeError):self.submit(fault=fault)
        self.file.parent.mkdir(parents=True);self.file.write_text('external edit')
        with self.assertRaises(writes.WriteStopped):self.submit()
        self.assertEqual('external edit',self.file.read_text())
    def test_pending_operation_blocks_next(self):
        def fault(stage,index):
            if stage=='after_database_commit':raise RuntimeError('pause')
        with self.assertRaises(RuntimeError):self.submit(fault=fault)
        self.operation+='-next'
        with self.assertRaises(writes.WriteStopped):self.submit()
    def test_new_schema_backup_restore(self):
        self.submit();backup=self.root/'backup.sqlite';restored=self.root/'restored.sqlite'
        with sqlite3.connect(str(self.path)) as a, sqlite3.connect(str(backup)) as b:a.backup(b)
        with sqlite3.connect(str(backup)) as a, sqlite3.connect(str(restored)) as b:a.backup(b)
        with sqlite3.connect(str(restored)) as c:
            self.assertEqual('ok',c.execute('PRAGMA integrity_check').fetchone()[0]);self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())
            saved=json.loads(c.execute('SELECT request_json FROM project_write_requests').fetchone()[0]);self.assertEqual(self.records,saved['records'])
            self.assertEqual('complete',c.execute('SELECT status FROM compatibility_deliveries').fetchone()[0])

class BusinessPatterns(unittest.TestCase):
    setUp=ProjectWrites.setUp
    tearDown=ProjectWrites.tearDown
    scalar=ProjectWrites.scalar
    submit=ProjectWrites.submit
    def prepare_body(self):
        self.submit();writes.set_write_target('ai-reader','project-db',self.path)
        self.capture={'recordType':'article-content','originalUrl':'https://example.test/a','resolvedUrl':'https://example.test/a','contentStatus':'verified','contentText':'Example saved body.','contentMarkdown':'Example saved body.','contentLength':19,'fetchedAt':'2026-09-17T01:01:00Z','contentCompleteness':'full','contentType':'article'}
        return business.submit('phase7-capture-one','capture',dict(day='2026-09-17',record=self.capture),self.path,self.root)
    def test_capture_version_and_missing_body(self):
        self.prepare_body();changed=dict(self.capture,contentText='Changed body.',contentMarkdown='Changed body.',contentLength=13,fetchedAt='2026-09-17T01:02:00Z')
        business.submit('phase7-capture-two','capture',dict(day='2026-09-17',record=changed),self.path,self.root)
        self.assertEqual(2,self.scalar('SELECT count(*) FROM article_content_versions'))
        self.assertEqual(2,self.scalar('SELECT count(*) FROM content_fetch_attempts'))
        missing=dict(changed,contentText='',contentMarkdown='',contentHash='f'*64,contentLength=100,fetchedAt='2026-09-17T01:03:00Z')
        business.submit('phase7-missing-one','capture',dict(day='2026-09-17',record=missing),self.path,self.root)
        self.assertEqual(2,self.scalar('SELECT count(*) FROM content_payloads'))
        self.assertEqual(1,self.scalar("SELECT count(*) FROM content_fetch_attempts WHERE body_integrity='held_missing_body' AND status='unverified' AND source_status='verified' AND version_id IS NULL"))
    def test_cache_projection_is_recoverable_from_db(self):
        self.prepare_body()
        raw=dict(self.capture,contentHash=db.checksum(b'Example saved body.'))
        entry=dict(original_url=raw['originalUrl'],resolved_url=raw['resolvedUrl'],article_key='capture-key',status='verified',content_text=raw['contentText'],content_markdown=raw['contentMarkdown'],body_length=raw['contentLength'],content_hash=raw['contentHash'],processed_at=raw['fetchedAt'])
        packet=dict(day='2026-09-17',record=raw,cacheEntry=entry,syncContents=False)
        result=business.submit('phase7-cache-one','capture',packet,self.path,self.root)
        cache=self.root/'content/article-body-captures/backfill-state.json'
        self.assertEqual(entry,json.loads(cache.read_text())['entries'][raw['originalUrl']])
        with sqlite3.connect(str(self.path)) as c:
            reference=json.loads(c.execute("SELECT payload_json FROM compatibility_deliveries WHERE operation_id='phase7-cache-one' AND target LIKE '%backfill-state.json'").fetchone()[0])
        self.assertNotIn('text',reference)
        restored=self.root/'restored-cache.sqlite'
        with sqlite3.connect(str(self.path)) as a,sqlite3.connect(str(restored)) as b:a.backup(b)
        self.assertEqual(cache.read_text(),business.render_compatibility_file(reference,restored))
        self.assertEqual(result,business.submit('phase7-cache-one','capture',packet,self.path,self.root))

    def test_review_summary_versions_and_proposal(self):
        import article_review_facts as rules
        import project_readers
        self.prepare_body()
        for relative in rules.POLICY_FILES:
            target=self.root/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes((db.ROOT/relative).read_bytes())
        with db.connect(self.path,readonly=True) as c:
            _,articles,captures=project_readers.Reader(c).load_day('2026-09-17',[])
        raw=dict(reviewVersion=rules.REVIEW_VERSION,url=self.capture['originalUrl'],inputHash=rules.input_hash(articles[0]['article'],captures[self.capture['originalUrl']]),policyHash=rules.policy_hash(self.root),basis='body',taskStatus={k:'ready' for k in rules.TASKS},facts=[{'id':'f1','text':'Example saved body.','evidenceIds':['e1']}],entities=[{'name':'Example','kind':'other','factIds':['f1']}],evidence=[{'id':'e1','field':'contentText','start':0,'end':19,'quote':'Example saved body.'}],unresolved=[],reviewedBy='test-reviewer',reviewedAt='2026-09-17T01:05:00Z',sourceDate='2026-09-17')
        business.submit('phase7-review-one','reviews',dict(day='2026-09-17',records=[raw]),self.path,self.root)
        self.assertEqual(1,self.scalar('SELECT count(*) FROM review_evidence'))
        text='## Source-by-source Notes\n- [Example](https://example.test/a) - 本文確認: 確認済み - 要約: Summary one.\n'
        business.submit('phase7-summary-one','summary',dict(day='2026-09-17',text=text),self.path,self.root)
        business.submit('phase7-summary-two','summary',dict(day='2026-09-17',text=text.replace('one.','two.')),self.path,self.root)
        self.assertEqual(2,self.scalar('SELECT count(*) FROM article_summaries'))
        self.assertEqual(1,self.scalar('SELECT count(*) FROM article_summaries WHERE is_current=1'))
        proposal=dict(day='2026-09-17',directory='talent-index-proposals',document={'proposalVersion':1,'articles':[],'talents':[{'talent_id':'new','status':'pending'}],'articleTalents':[]})
        business.submit('phase7-proposal-one','proposal',proposal,self.path,self.root)
        self.assertEqual(2,self.scalar('SELECT count(*) FROM legacy_history_records'))
        self.assertEqual(0,self.scalar('SELECT count(*) FROM talents'))

class NativePatterns(unittest.TestCase):
    setUp=ProjectWrites.setUp
    tearDown=ProjectWrites.tearDown
    scalar=ProjectWrites.scalar
    def test_native_new_update_versions_and_compatibility(self):
        from unittest.mock import patch
        import project_compatibility as compat
        writes.set_write_target('dashboard','project-db',self.path)
        stamp='2026-09-17T01:00:00Z'
        article=dict(article_key='key-one',url='https://example.test/native',title='Native',excerpt='',source='test',published_at=stamp,last_seen_at=stamp)
        talent=dict(talent_id='person-one',display_name='Person',organization='First',aliases_json='["Alias"]',status='pending',search_enabled=False,auto_discovered=True,last_seen_at=stamp)
        relation=dict(relation_key='link-one',article_key='key-one',talent_id='person-one',matched_aliases_json='["Alias"]',matched_fields='body',evidence_text='Saved evidence',confidence=1.0,detection_method='reviewed',last_seen_at=stamp)
        classification=dict(article_key='key-one',article_type='news_article',primary_category='talent_activity',secondary_categories_json='["event"]',relevance='in_scope',confidence=1.0,evidence_text='Saved evidence',classification_method='reviewed',classified_at=stamp)
        feedback=dict(article_key='key-one',is_rejected=True,reason_code='irrelevant',reviewed_at=stamp,review_source='article-review-markdown')
        local=self.root/'legacy.sqlite'
        with sqlite3.connect(str(local)) as c:
            c.execute('CREATE TABLE data_table(id TEXT,name TEXT)')
            for name,row in [('articles',article),('talents',talent),('article_talents',relation),('article_classifications',classification),('article_feedback',feedback)]:
                c.execute('INSERT INTO data_table VALUES (?,?)',(name,name))
                columns=','.join(k+(' INTEGER' if isinstance(v,bool) else ' REAL' if isinstance(v,float) else ' TEXT') for k,v in row.items())
                c.execute('CREATE TABLE data_table_user_'+name+'('+columns+')')
        def deliver(delivery):
            payload=json.loads(delivery['payload_json']);data=payload['data'];name=delivery['target'];key=compat.KEYS[name]
            self.assertGreater(self.scalar('SELECT count(*) FROM project_write_requests'),0)
            with sqlite3.connect(str(local)) as c:
                found=c.execute('SELECT 1 FROM data_table_user_'+name+' WHERE '+key+'=?',(data[key],)).fetchone()
                if found:c.execute('UPDATE data_table_user_'+name+' SET '+','.join(k+'=?' for k in data)+' WHERE '+key+'=?',list(data.values())+[data[key]])
                else:c.execute('INSERT INTO data_table_user_'+name+'('+','.join(data)+') VALUES ('+','.join('?' for _ in data)+')',list(data.values()))
        with patch.dict(os.environ,{'N8N_DATABASE_PATH':str(local)}),patch.object(compat,'deliver',deliver):
            proposal=dict(proposalVersion=1,proposalDate='2026-09-17',articles=[article],talents=[talent],articleTalents=[relation])
            first=business.submit('phase7-native-one','talent',dict(proposal=proposal),self.path,self.root)
            self.assertEqual(first,business.submit('phase7-native-one','talent',dict(proposal=proposal),self.path,self.root))
            revised=copy.deepcopy(proposal);revised['talents'][0].update(organization='Second',aliases_json='["Alias","Other"]')
            business.submit('phase7-native-two','talent',dict(proposal=revised),self.path,self.root)
            self.assertEqual(2,self.scalar('SELECT count(*) FROM talent_aliases'))
            self.assertEqual('proposed',self.scalar('SELECT state FROM article_talents'))
            cp=dict(proposalVersion=1,proposalDate='2026-09-17',classifications=[classification])
            business.submit('phase7-class-one','classification',dict(proposal=cp),self.path,self.root)
            cp=copy.deepcopy(cp);cp['classifications'][0]['evidence_text']='Revised evidence'
            business.submit('phase7-class-two','classification',dict(proposal=cp),self.path,self.root)
            self.assertEqual(2,self.scalar('SELECT count(*) FROM article_classifications'))
            business.submit('phase7-feedback-one','feedback',dict(feedback=feedback),self.path,self.root)
            changed=dict(feedback,is_rejected=False,reason_code='approved',reviewed_at='2026-09-17T01:02:00Z')
            business.submit('phase7-feedback-two','feedback',dict(feedback=changed),self.path,self.root)
            self.assertEqual(2,self.scalar('SELECT count(*) FROM article_feedback'))
            self.assertEqual(1,self.scalar('SELECT count(*) FROM article_feedback WHERE is_current=1'))
            with sqlite3.connect(str(self.path)) as c:self.assertEqual([],c.execute('PRAGMA foreign_key_check').fetchall())

class ServicePattern(unittest.TestCase):
    setUp=ProjectWrites.setUp
    tearDown=ProjectWrites.tearDown
    def test_http_auth_save_status_replay(self):
        import threading
        import urllib.request
        import urllib.error
        from unittest.mock import patch
        import autoarticle_db_service as service
        server=service.make_server(self.path,'t'*40,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        origin='http://127.0.0.1:'+str(server.server_address[1])
        try:
            with self.assertRaises(urllib.error.HTTPError):urllib.request.urlopen(origin+'/health')
            original=business.submit_collection
            with patch.object(business,'submit_collection',side_effect=lambda op,records,path:original(op,records,path,self.root)):
                req=urllib.request.Request(origin+'/v1/write/collection',data=json.dumps({'operationId':self.operation,'records':self.records}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+'t'*40})
                with urllib.request.urlopen(req) as response:first=json.load(response)
                with urllib.request.urlopen(req) as response:self.assertEqual(first,json.load(response))
                req=urllib.request.Request(origin+'/v1/write-status/'+self.operation,headers={'Authorization':'Bearer '+'t'*40})
                with urllib.request.urlopen(req) as response:state=json.load(response)
                self.assertEqual('complete',state['status']);self.assertEqual(0,state['pendingDeliveries'])
        finally:server.shutdown();server.server_close();worker.join()

if __name__=='__main__':unittest.main()
