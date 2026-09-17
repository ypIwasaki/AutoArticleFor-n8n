import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import autoarticle_n8n as n8n
import autoarticle_ops as ops
from autoarticle_progress import Blocked

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); (self.root/'data').mkdir()
        self.c = sqlite3.connect(str(self.root/'data/autoarticle.sqlite'))
        self.addCleanup(self.c.close)
        self.c.executescript("CREATE TABLE collection_runs(run_date TEXT); CREATE TABLE compatibility_deliveries(status TEXT); CREATE TABLE sync_runs(id TEXT);")
        self.client = Mock(base='http://127.0.0.1:5678')
        self.op = ops.Operations(self.root,'2026-09-17',self.client)
        self.op.workflow = Mock(return_value=({'id':'wf','active':True},True))
        self.op.verify_collection = Mock(return_value=({},2))
        self.parent = {'id':'183','workflowId':'wf','status':'error','retryOf':None}
        self.rows = [self.parent]
        self.detail = dict(self.parent, data={'resultData':{'lastNodeExecuted':'Read RSS Search Results','error':{'message':'read ETIMEDOUT'},'runData':{'Read RSS Search Results':[{'error':{}}]}}})
        self.client.request.return_value=self.detail
        self.op.progress.record('collect','submission_unknown',files={},workflowId='wf')
        a=patch.object(n8n,'executions',side_effect=lambda *args:(self.rows,[]));a.start();self.addCleanup(a.stop)
        a=patch('autoarticle_progress.today',return_value='2026-09-17');a.start();self.addCleanup(a.stop)

    def test_ready_without_mutation(self):
        before=self.op.progress.file.read_bytes()
        self.assertEqual(n8n.collection_retry_preflight(self.op,'183')['status'],'ready')
        self.assertEqual(before,self.op.progress.file.read_bytes())
        self.assertEqual(self.client.request.call_count,1)
        self.assertTrue(self.client.request.call_args.kwargs['api'])

    def test_reject_write_node_unknown_intent_and_db_save(self):
        self.detail['data']['resultData']['runData']['Save Collection to Project DB']=[]
        with self.assertRaisesRegex(Blocked,'may_have_saved'):n8n.collection_retry_preflight(self.op,'183')
        del self.detail['data']['resultData']['runData']['Save Collection to Project DB']
        self.c.execute("INSERT INTO sync_runs VALUES ('db-collect-wf-183')");self.c.commit()
        with self.assertRaisesRegex(Blocked,'write_request_exists'):n8n.collection_retry_preflight(self.op,'183')
        self.c.execute('DELETE FROM sync_runs');self.c.commit()
        self.op.progress.record('collect','submission_unknown',files={},retryOf='183')
        with self.assertRaisesRegex(Blocked,'unknown_do_not_resubmit'):n8n.collection_retry_preflight(self.op,'183')

    def test_reject_unrelated_or_failed_child(self):
        self.rows.append({'id':'184','status':'error','retryOf':None})
        with self.assertRaisesRegex(Blocked,'unrelated'):n8n.collection_retry_preflight(self.op,'183')
        self.rows[-1]['retryOf']='183'
        with self.assertRaisesRegex(Blocked,'already_exists'):n8n.collection_retry_preflight(self.op,'183')

    def test_single_post_linked_retry_and_reuse(self):
        def request(path,body=None,**kwargs):
            if body is None:return self.detail
            self.assertEqual(path,'/executions/183/retry');self.assertEqual(body,{'loadWorkflow':True})
            self.assertEqual(self.op.progress.load()['steps']['collect']['retryOf'],'183')
            child={'id':'184','workflowId':'wf','status':'success','retryOf':'183'}
            self.rows.append(child);return child
        self.client.request.side_effect=request
        result=self.op.retry_collection('183');self.assertEqual(result['articles'],2)
        self.assertEqual(self.op.progress.load()['steps']['collect']['executionId'],'184')
        count=self.client.request.call_count
        self.assertEqual(self.op.retry_collection('183')['executionId'],'184')
        self.assertEqual(self.client.request.call_count,count)
        self.assertEqual(n8n.collection_success(self.rows,self.op.progress.load()['steps']['collect']),'184')

    def test_lost_reply_never_resubmits(self):
        self.client.request.side_effect=[self.detail,Blocked('connection_unavailable_or_timeout')]
        with self.assertRaises(Blocked):self.op.retry_collection('183')
        count=self.client.request.call_count
        with self.assertRaisesRegex(Blocked,'unknown_do_not_resubmit'):self.op.retry_collection('183')
        self.assertEqual(self.client.request.call_count,count)

    def test_lineage_rejects_extra_success(self):
        self.rows.extend([{'id':'184','status':'success','retryOf':'183'},{'id':'185','status':'success','retryOf':None}])
        with self.assertRaisesRegex(Blocked,'lineage_invalid'):n8n.collection_success(self.rows,{'executionId':'184','retryOf':'183'})

    def test_rss_provenance_and_per_feed_retries(self):
        workflow=json.loads((ROOT/'n8n/workflows/daily-keyword-news-summary.workflow.json').read_text())
        nodes={n['name']:n for n in workflow['nodes']}
        rss=nodes['Fetch One RSS Feed']
        self.assertEqual((rss['retryOnFail'],rss['maxTries'],rss['waitBetweenTries']),(True,3,5000))
        self.assertNotIn('continueOnFail',rss);self.assertNotIn('onError',rss)
        self.assertEqual(nodes['Read RSS Search Results']['parameters']['batchSize'],1)
        result=subprocess.run(['node',str(ROOT/'tests/rss_retry_fixture.cjs')],cwd=str(ROOT),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('passed',result.stdout)

if __name__=='__main__':unittest.main()
