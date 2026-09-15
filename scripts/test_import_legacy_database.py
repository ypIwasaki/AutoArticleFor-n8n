"""Synthetic phase 3 conservation tests; no real source database is opened."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import project_database as db
import import_legacy_database as imp

class LegacyImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'target.sqlite';db.migrate(self.path)
    def tearDown(self):self.temp.cleanup()
    def fixture(self):
        source=self.root/'snapshot';(source/'files').mkdir(parents=True)
        with sqlite3.connect(str(source/'n8n.sqlite')) as c:
            c.execute('CREATE TABLE data_table(id TEXT,name TEXT)')
            for name in imp.baseline.KEYS:
                c.execute('INSERT INTO data_table VALUES (?,?)',(name,name))
                if name=='articles':c.execute('CREATE TABLE data_table_user_articles(id INTEGER,article_key TEXT,url TEXT,title TEXT,excerpt TEXT,source TEXT,published_at TEXT,createdAt TEXT,updatedAt TEXT)')
                elif name=='article_contents':c.execute('CREATE TABLE data_table_user_article_contents(id INTEGER,article_key TEXT,original_url TEXT,resolved_url TEXT,content_text TEXT,content_hash TEXT,content_status TEXT,fetched_at TEXT)')
                else:c.execute('CREATE TABLE data_table_user_'+name+'(id INTEGER)')
            stamp='2026-09-14T00:00:00Z';body='Saved body';h=db.checksum(body.encode())
            for i,key,url,title in [(1,'key-one','https://example.test/one','One'),(2,'key-a','https://example.test/multi','A'),(3,'key-b','https://example.test/multi','B')]:
                c.execute('INSERT INTO data_table_user_articles VALUES (?,?,?,?,?,?,?,?,?)',(i,key,url,title,'','test',stamp,stamp,stamp))
            for row in [(1,'key-one','https://example.test/one'),(2,'unknown-one','https://example.test/one'),(3,'unknown-multi','https://example.test/multi')]:
                c.execute('INSERT INTO data_table_user_article_contents VALUES (?,?,?,?,?,?,?,?)',(*row,row[2],body,h,'verified',stamp))
        (source/'input-files.json').write_text('[]')
        (source/'phase1-result.json').write_text(json.dumps({'status':'complete','backup_sha256':imp.baseline.sha(source/'n8n.sqlite'),'finished_at':stamp}))
        return source
    def test_conservative_identity_policy(self):
        body='Saved body';h=db.checksum(body.encode());stamp='2026-09-14T00:00:00Z'
        r=dict(article_key='old',original_url='https://example.test/a',content_text=body,content_hash=h)
        a=dict(article_key='candidate',title='Title',published_at=stamp)
        e=dict(article_key='old',original_url=r['original_url'],title='Title',published_at=stamp,content_text=body,content_hash=h)
        self.assertTrue(imp.assess_content(r,[a],[e])[0])
        self.assertFalse(imp.assess_content(r,[a,a],[e])[0])
        self.assertFalse(imp.assess_content(r,[a],[])[0])
        self.assertFalse(imp.assess_content(r,[a],[dict(e,content_text='different')])[0])
        self.assertFalse(imp.assess_content(r,[a],[dict(e,published_at='2026-09-15T00:00:00Z')])[0])
        self.assertEqual(imp.ident('held-article','table','3'),imp.ident('held-article','table','3'))
        self.assertNotEqual(imp.ident('held-article','table','3'),imp.ident('held-article','table','4'))
    def test_import_preserves_held_sources_and_replays_zero_rows(self):
        snapshot=self.fixture();first=imp.import_snapshot(snapshot,self.path)
        self.assertEqual(first['source_records'],6)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM articles WHERE identity_state='held'").fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM articles').fetchone()[0],5)
            self.assertEqual(c.execute('SELECT count(*) FROM content_payloads').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM content_fetch_attempts WHERE status='verified'").fetchone()[0],3)
            self.assertEqual(c.execute('SELECT count(*) FROM article_identity_assessments').fetchone()[0],3)
            self.assertEqual(c.execute("SELECT count(*) FROM article_source_provenance WHERE source_table='n8n:data_table_user_article_contents' AND old_article_key IS NOT NULL AND stored_content_hash IS NOT NULL AND fetch_status='verified'").fetchone()[0],3)
            before=list(c.iterdump())
        second=imp.import_snapshot(snapshot,self.path)
        self.assertTrue(second['replayed']);self.assertTrue(all(n==0 for n in second['row_deltas'].values()))
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:self.assertEqual(before,list(c.iterdump()))
    def test_failure_rolls_back_and_same_input_can_resume(self):
        snapshot=self.fixture()
        with patch.object(imp.Importer,'reviews',side_effect=RuntimeError('injected interruption')):
            with self.assertRaises(RuntimeError):imp.import_snapshot(snapshot,self.path)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            for table in ('articles','source_records','migration_runs','content_payloads'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.assertFalse(imp.import_snapshot(snapshot,self.path)['replayed'])
    def test_file_archive_and_declared_count_failure(self):
        snapshot=self.fixture();relative='content/structured-records/2026-09-14.jsonl';file=snapshot/'files'/relative;file.parent.mkdir(parents=True)
        row={'recordType':'run','runDate':'2026-09-14','articleCount':1,'generatedAt':'2026-09-14T00:00:00Z'}
        file.write_text(json.dumps(row)+'\n')
        manifest=[{'path':relative,'size':file.stat().st_size,'sha256':imp.baseline.sha(file)}]
        (snapshot/'input-files.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError,'Declared collection count mismatch'):imp.import_snapshot(snapshot,self.path)
        row['articleCount']=0;file.write_text(json.dumps(row)+'\n');manifest[0].update(size=file.stat().st_size,sha256=imp.baseline.sha(file));(snapshot/'input-files.json').write_text(json.dumps(manifest))
        imp.import_snapshot(snapshot,self.path)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual(c.execute('SELECT content FROM migration_source_files').fetchone()[0],file.read_bytes())
    def add_files(self,snapshot,documents):
        manifest=[]
        for relative,content in documents.items():
            p=snapshot/'files'/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
            manifest.append(dict(path=relative,size=p.stat().st_size,sha256=imp.baseline.sha(p)))
        (snapshot/'input-files.json').write_text(json.dumps(manifest))
    def test_corroboration_and_ambiguous_cache_stays_held(self):
        snapshot=self.fixture();stamp='2026-09-14T00:00:00Z';body='Saved body'
        def entry(key,url,title):return dict(article_key=key,original_url=url,title=title,published_at=stamp,status='verified',content_text=body,content_hash=db.checksum(body.encode()),processed_at=stamp,retry_after=1789344000)
        obj={'entries':{'one':entry('unknown-one','https://example.test/one','One'),'multi':entry('unknown-multi','https://example.test/multi','A')},'resolvedUrls':{},'schemaVersion':1}
        self.add_files(snapshot,{'content/article-body-captures/backfill-state.json':json.dumps(obj)})
        imp.import_snapshot(snapshot,self.path)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM article_identity_assessments WHERE strategy='corroborated'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM article_identity_assessments WHERE strategy='held'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM content_fetch_attempts WHERE retry_after IS NOT NULL").fetchone()[0],2)
    def test_review_quotes_and_statuses_preserved(self):
        snapshot=self.fixture();stamp='2026-09-14T00:00:00Z'
        article={'title':'One','url':'https://example.test/one','publishedAt':stamp,'excerpt':'','source':'test'}
        capture={'articleKey':'key-one','originalUrl':article['url'],'contentStatus':'verified','contentText':'Saved body','fetchedAt':stamp}
        run={'recordType':'run','runDate':'2026-09-14','articleCount':1,'generatedAt':stamp}
        row={'recordType':'article','runDate':'2026-09-14','article':article}
        record=dict(reviewVersion=1,url=article['url'],inputHash=imp.review_rules.input_hash(article,capture),policyHash='saved-policy',basis='body',taskStatus={t:'ready' for t in imp.review_rules.TASKS},facts=[dict(id='f1',text='Synthetic fact',evidenceIds=['e1'])],entities=[dict(name='Synthetic',kind='other',factIds=['f1'])],evidence=[dict(id='e1',field='contentText',start=0,end=5,quote='Saved')],unresolved=[],reviewedBy='fixture',reviewedAt=stamp,sourceDate='2026-09-14')
        docs={'content/structured-records/2026-09-14.jsonl':json.dumps(run)+'\n'+json.dumps(row)+'\n','content/article-body-captures/2026-09-14.jsonl':json.dumps(capture)+'\n','content/article-review-facts/2026-09-14.jsonl':json.dumps(record)+'\n'}
        self.add_files(snapshot,docs);imp.import_snapshot(snapshot,self.path)
        with contextlib.closing(db.connect(self.path,readonly=True)) as c:
            self.assertEqual(c.execute('SELECT quote,start_offset,end_offset FROM review_evidence').fetchone()[:],('Saved',0,5))
            self.assertEqual(c.execute("SELECT count(*) FROM review_task_statuses WHERE status='ready'").fetchone()[0],3)
            self.assertEqual(c.execute('SELECT count(*) FROM review_entity_facts').fetchone()[0],1)
            self.assertEqual(json.loads(c.execute('SELECT raw_json FROM review_records').fetchone()[0]),record)
    def check_inconsistent_body(self,text,state):
        snapshot=self.fixture()
        with sqlite3.connect(snapshot/'n8n.sqlite') as c:
            c.execute('ALTER TABLE data_table_user_article_contents ADD COLUMN content_length INTEGER')
            c.execute('ALTER TABLE data_table_user_article_contents ADD COLUMN content_path TEXT')
            c.execute("UPDATE data_table_user_article_contents SET content_text=?,content_length=10,content_path='missing.jsonl' WHERE id=1",(text,))
        meta=json.loads((snapshot/'phase1-result.json').read_text());meta['backup_sha256']=imp.baseline.sha(snapshot/'n8n.sqlite');(snapshot/'phase1-result.json').write_text(json.dumps(meta))
        imp.import_snapshot(snapshot,self.path)
        with contextlib.closing(db.connect(self.path)) as c:
            r=c.execute("SELECT f.*,s.state,s.input_hash,s.raw_json AS source_raw,a.identity_state FROM source_records s JOIN content_fetch_attempts f ON f.id=s.target_id JOIN articles a ON a.id=f.article_id WHERE s.source_path='n8n:data_table_user_article_contents' AND s.record_position='1'").fetchone()
            self.assertEqual((r['body_integrity'],r['status'],r['source_status'],r['state'],r['identity_state']),(state,'unverified','verified','held','held'))
            self.assertIsNone(r['version_id']);self.assertEqual(r['stored_body_length'],10);self.assertEqual(r['source_content_path'],'missing.jsonl')
            raw=json.loads(r['source_raw']);self.assertEqual(raw,json.loads(r['raw_json']));self.assertEqual(r['input_hash'],imp.hashrow(raw));self.assertEqual(r['stored_body_hash'],raw['content_hash']);self.assertEqual(raw['content_text'],text)
            self.assertEqual(c.execute('SELECT count(*) FROM content_payloads').fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM consolidation_conflicts WHERE kind='body_integrity'").fetchone()[0],1)
            with self.assertRaises(sqlite3.IntegrityError):c.execute("UPDATE content_fetch_attempts SET status='verified' WHERE id=?",(r['id'],))
        self.assertTrue(all(v==0 for v in imp.import_snapshot(snapshot,self.path)['row_deltas'].values()))
    def test_empty_body_with_nonempty_saved_claim_is_held(self):
        self.check_inconsistent_body('','held_missing_body')
    def test_nonempty_body_hash_mismatch_is_held(self):
        self.check_inconsistent_body('Different body','held_hash_mismatch')

    def test_tampered_snapshot_stops_before_import(self):
        snapshot=self.fixture();with_path=snapshot/'n8n.sqlite'
        with_path.write_bytes(with_path.read_bytes()+b'tampered')
        with self.assertRaisesRegex(RuntimeError,'Snapshot not verified'):imp.import_snapshot(snapshot,self.path)

if __name__=='__main__':unittest.main()
