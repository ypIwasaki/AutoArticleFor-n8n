import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project_database as db
import project_write_outbox as writes
import project_legacy_retirement as retire
import project_business_writes as business
import project_virtual_files as virtual
import project_compatibility as compat
from autoarticle_progress import Progress


class Retirement(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=os.environ.get('AUTOARTICLE_TEST_TMPDIR'))
        self.root=Path(self.temp.name);self.path=self.root/'data/autoarticle.sqlite'
        db.migrate(self.path)
        with db.connect(self.path) as c:c.execute("UPDATE cutover_state SET read_source='project-db',write_target='project-db'")
        retire.set_mode('on-demand',self.path)
        self.records=[dict(recordType='run',runDate='2026-09-17',generatedAt='2026-09-17T01:00:00Z',capturedArticleCount=1,workflowExecutionId='ordinary-1'),dict(recordType='article',runDate='2026-09-17',articleIndex=1,article=dict(title='Example',url='https://example.test/a',excerpt='source',publishedAt='2026-09-17T00:00:00Z'))]
    def tearDown(self):self.temp.cleanup()
    def scalar(self,sql):
        with sqlite3.connect(str(self.path)) as c:return c.execute(sql).fetchone()[0]
    def commit_files(self,operation,text,fail=None):
        def build(unit,request):
            unit.file('content/test/state.json',text)
            unit.file('content/test/report.md','# '+text)
        return writes.commit(operation,dict(version=writes.VERSION,kind='test',text=text),self.path,self.root,build,fault=fail)

    def test_normal_db_only_read_export_replay_and_restore(self):
        result=business.submit_collection('retirement-collection',self.records,self.path,self.root)
        self.assertEqual('on-demand',result['compatibility'])
        self.assertFalse((self.root/'content/structured-records/2026-09-17.jsonl').exists())
        self.assertEqual(self.records,virtual.collection_records('2026-09-17',self.path))
        self.assertEqual('Example',virtual.collection_output('2026-09-17',self.path)['articles'][0]['title'])
        state=writes.status('retirement-collection',self.path)
        self.assertEqual((0,1),(state['pendingDeliveries'],state['deferredDeliveries']))
        self.assertEqual(result,business.submit_collection('retirement-collection',self.records,self.path,self.root))
        self.assertEqual(1,self.scalar('SELECT count(*) FROM article_occurrences'))
        progress=Progress(self.root,'2026-09-17','local')
        self.assertTrue(progress.fingerprint(progress.generated()['structured-records']).startswith('project-db:'))
        export=self.root/'export';retire.export('retirement-collection',export,self.path)
        self.assertEqual(self.records,[json.loads(x) for x in (export/'content/structured-records/2026-09-17.jsonl').read_text().splitlines()])
        self.assertEqual(1,writes.status('retirement-collection',self.path)['deferredDeliveries'])
        with sqlite3.connect(str(self.path)) as a,sqlite3.connect(str(self.root/'restore.sqlite')) as b:a.backup(b)
        self.assertEqual(self.records,virtual.collection_records('2026-09-17',self.root/'restore.sqlite'))
        self.assertEqual(state['deferredDeliveries'],writes.status('retirement-collection',self.root/'restore.sqlite')['deferredDeliveries'])
        changed=copy.deepcopy(self.records);changed[1]['article']['title']='Changed'
        with self.assertRaises(db.Conflict):business.submit_collection('retirement-collection',changed,self.path,self.root)

    def test_human_output_and_explicit_rollback_chain(self):
        first=self.commit_files('retirement-first','one');self.commit_files('retirement-second','two')
        self.assertFalse((self.root/'content/test/state.json').exists())
        self.assertEqual('# two',(self.root/'content/test/report.md').read_text())
        with self.assertRaises(writes.WriteStopped):writes.set_write_target('ai-reader','legacy',self.path)
        with self.assertRaises(RuntimeError):retire.set_mode('automatic',self.path)
        retire.set_mode('maintenance',self.path)
        with self.assertRaises(writes.WriteStopped):self.commit_files('retirement-third','three')
        def lost(stage,ordinal):raise RuntimeError('lost acknowledgement')
        with self.assertRaises(RuntimeError):retire.catch_up(self.path,self.root,fault=lost)
        self.assertEqual(0,retire.catch_up(self.path,self.root)['remaining'])
        self.assertEqual('two',(self.root/'content/test/state.json').read_text())
        self.assertEqual(first,self.commit_files('retirement-first','one'))
        self.assertEqual(first,writes.drain('retirement-first',self.path,self.root))
        writes.set_write_target('ai-reader','legacy',self.path)
        retire.set_mode('automatic',self.path)

    def test_table_chain_and_atomic_schedule_rollback(self):
        legacy={'article_key':'key','title':'initial','excerpt':'initial'}
        def lookup(name,key):return 'table',dict(legacy)
        def build(unit,request):compat.enqueue(unit,'articles',request['data'])
        def send(op,changes,fault=None):return writes.commit(op,dict(version=writes.VERSION,kind='test',data=dict(article_key='key',**changes)),self.path,self.root,build,fault=fault)
        with patch.object(compat,'current',lookup):
            send('table-first',{'title':'one'})
            send('table-second',{'excerpt':'two','title':'two'})
            self.assertEqual('initial',legacy['title'])
            def fault(stage,index):
                if stage=='after_database_commit':raise RuntimeError('lost response')
            with self.assertRaises(RuntimeError):send('table-third',{'title':'three'},fault)
            send('table-third',{'title':'three'})
            retire.set_mode('maintenance',self.path)
            def deliver(row):
                value=json.loads(row['payload_json']);self.assertEqual({k:legacy.get(k) for k in value['data']},row.get('expected_before',value['before']));legacy.update(value['data'])
            retire.catch_up(self.path,self.root,deliver)
        self.assertEqual(('three','two'),(legacy['title'],legacy['excerpt']))
        retire.set_mode('on-demand',self.path)
        def broken(unit,request):unit.file('content/test/broken.json','new');raise RuntimeError('partial')
        with self.assertRaises(RuntimeError):writes.commit('retirement-broken',dict(version=writes.VERSION,kind='test'),self.path,self.root,broken)
        self.assertEqual(0,self.scalar("SELECT count(*) FROM compatibility_delivery_schedule WHERE operation_id='retirement-broken'"))
        self.assertEqual(0,self.scalar("SELECT count(*) FROM project_write_requests WHERE operation_id='retirement-broken'"))
        self.assertEqual(0,self.scalar('SELECT count(*) FROM pragma_foreign_key_check'))

    def test_collection_verification_and_human_workflow_without_jsonl(self):
        import base64
        import subprocess
        from unittest.mock import Mock
        import autoarticle_n8n
        result=business.submit_collection('collection-verify',self.records,self.path,self.root)
        progress=Progress(self.root,'2026-09-17','local')
        markdown=[]
        for relative in progress.generated().values():
            if relative.endswith('.jsonl'):continue
            path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('# DB output')
            markdown.append({'json':{'relativePath':relative,'date':progress.date},'binary':{'data':{'data':base64.b64encode(path.read_bytes()).decode()}}})
        def node(items):return [{'data':{'main':[items]}}]
        execution=dict(workflowId='workflow',status='success',startedAt='2026-09-17T00:59:00Z',stoppedAt='2026-09-17T01:01:00Z',data={'resultData':{'runData':{
            'Build Structured Records':node([{'json':{'date':progress.date,'capturedArticleCount':1}}]),
            'Save Collection to Project DB':node([{'json':result}]),'Build Markdown Files':node(markdown)}}})
        client=Mock();client.request.return_value=execution
        files,count=autoarticle_n8n.verify_collection(client,progress,'workflow','ordinary-1')
        self.assertEqual(1,count);self.assertTrue(files[progress.generated()['structured-records']].startswith('project-db:'))
        workflow=json.loads((Path(__file__).resolve().parents[1]/'n8n/workflows/daily-keyword-news-summary.workflow.json').read_text())
        code=next(n['parameters']['jsCode'] for n in workflow['nodes'] if n['name']=='Continue After DB Collection Save')
        payload=dict(result,collection=virtual.collection_output(progress.date,self.path))
        program='const x='+json.dumps(payload)+'; const out=new Function("$json",'+json.dumps(code)+')(x); if (!out[0].json.digestMarkdown.includes("Example")) throw Error("DB article not rendered"); console.log("DB Markdown passed");'
        response=subprocess.run(['node','-e',program],text=True,capture_output=True)
        self.assertEqual(0,response.returncode,response.stderr)

    def test_capture_reads_db_cache_and_pacing_without_legacy_files(self):
        import capture_article_contents as capture
        business.submit_collection('capture-input',self.records,self.path,self.root)
        state=dict(hosts={'example.test':dict(interval=10,until=123,next=456)},globalNext=789)
        business.submit('pacing-input','runtime',dict(name='rate-limit-state.json',value=state),self.path,self.root)
        with patch.object(capture,'ROOT',self.root),patch.object(capture,'DIR',self.root/'content/article-body-captures'),patch.object(capture,'project_writes_enabled',return_value=True):
            fetch=capture.Fetch(30,10,2,capture.DIR/'rate-limit-state.json')
            self.assertEqual(state['hosts'],fetch.hosts);self.assertEqual(789,fetch.global_next)
            self.assertEqual('Example',capture.load_record_articles('2026-09-17')['https://example.test/a'].title)
        self.assertFalse((self.root/'content/article-body-captures/rate-limit-state.json').exists())

if __name__=='__main__':unittest.main()
