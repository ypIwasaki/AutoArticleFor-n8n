"""Isolated phase 2 contract tests. Never opens the user's legacy database."""
import contextlib
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import project_database as db
import autoarticle_db_service as service

class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.path=self.root/'project.sqlite'
        db.migrate(self.path)
    def tearDown(self):
        self.temp.cleanup()
    def article(self,id='article-1'):
        return dict(id=id,title='Title',url='https://example.test/item',created_at=db.now(),updated_at=db.now())
    def test_repeat_and_backup_restore(self):
        self.assertEqual(db.migrate(self.path),{'applied':[]})
        records={'article':[self.article()]}
        first=db.save_operation('articles','operation-1',records,self.path)
        self.assertEqual(first,db.save_operation('articles','operation-1',records,self.path))
        with self.assertRaises(db.Conflict): db.save_operation('articles','operation-1',{'article':[self.article('changed')]},self.path)
        snap=self.root/'backup.sqlite'
        db.backup(snap,self.path)
        result=db.restore_check(snap,self.root/'restored.sqlite')
        self.assertEqual(result['integrity'],'ok')
        with db.connect(self.root/'restored.sqlite',readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM articles').fetchone()[0],1)
            self.assertEqual(c.execute('SELECT count(*) FROM sync_runs').fetchone()[0],1)
        with self.assertRaises(ValueError): db.backup(snap,self.path)
    def test_atomic_invalid_reference_and_retry(self):
        records={'article':[self.article()], 'identifier':[dict(id='identifier-1',article_id='missing',source='legacy',kind='legacy_key',value='key',match_state='exact')]}
        with self.assertRaises(sqlite3.IntegrityError): db.save_operation('articles','operation-fail',records,self.path)
        with db.connect(self.path,readonly=True) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM articles').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM sync_runs').fetchone()[0],0)
        records['identifier'][0]['article_id']='article-1'
        self.assertEqual(db.save_operation('articles','operation-fail',records,self.path)['successCount'],2)
    def test_constraints_and_immutable_id(self):
        db.save_operation('articles','operation-1',{'article':[self.article()]},self.path)
        c=db.connect(self.path)
        try:
            self.assertEqual(c.execute('PRAGMA foreign_keys').fetchone()[0],1)
            self.assertEqual(c.execute('PRAGMA journal_mode').fetchone()[0],'wal')
            with self.assertRaises(sqlite3.IntegrityError): c.execute("UPDATE articles SET id='changed' WHERE id='article-1'")
            with self.assertRaises(sqlite3.IntegrityError): c.execute("UPDATE articles SET updated_at='2026-09-14T10:00:00+09:00'")
            with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO talents(id,display_name,status,search_enabled,auto_discovered,raw_json) VALUES ('t','x','unknown',0,0,'{}')")
            with self.assertRaises(sqlite3.IntegrityError): c.execute("INSERT INTO talents(id,display_name,status,search_enabled,auto_discovered,raw_json) VALUES ('t','x','pending',0,0,'broken')")
        finally: c.close()
    def test_migration_transaction_and_checksum(self):
        directory=self.root/'migrations'
        shutil.copytree(db.MIGRATIONS,directory)
        (directory/'900_test.sql').write_text('CREATE TABLE test_upgrade(id TEXT PRIMARY KEY);\n')
        self.assertEqual(db.migrate(self.path,directory)['applied'],['900'])
        (directory/'901_failure.sql').write_text('CREATE TABLE should_rollback(id TEXT);\nINSERT INTO absent_table VALUES (1);\n')
        with self.assertRaises(sqlite3.OperationalError): db.migrate(self.path,directory)
        with db.connect(self.path,readonly=True) as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='should_rollback'").fetchone())
            self.assertEqual(c.execute('SELECT count(*) FROM schema_migrations').fetchone()[0],len(list(db.MIGRATIONS.glob('*.sql')))+1)
        (directory/'001_initial.sql').write_text('-- modified\n'+(directory/'001_initial.sql').read_text())
        with self.assertRaises(ValueError): db.migrate(self.path,directory)
    def test_no_proposal_approval_or_sql(self):
        with self.assertRaises(ValueError): db.save_operation('talent-proposals','operation-1',{'talent':[dict(id='talent-1',display_name='x',status='approved',search_enabled=1,auto_discovered=1,raw_json='{}')]},self.path)
        with self.assertRaises(ValueError): db.save_operation('articles','operation-2',{'sql':['DROP TABLE articles']},self.path)
        with self.assertRaises(ValueError): db.save_operation('articles','bad',{'article':[self.article()]},self.path)
    def test_legacy_database_rejected_before_wal(self):
        path=self.root/'legacy.sqlite'
        with sqlite3.connect(path) as c: c.execute('CREATE TABLE data_table(id TEXT)')
        before=path.read_bytes()
        with self.assertRaises(ValueError): db.connect(path)
        self.assertEqual(before,path.read_bytes())
    def test_http_contract(self):
        token='test-only-'+('a'*40)
        srv=service.make_server(self.path,token,0)
        thread=threading.Thread(target=srv.serve_forever,daemon=True)
        thread.start()
        output=io.StringIO()
        def request(method,path,payload=None,auth=True,extra=None):
            c=http.client.HTTPConnection('127.0.0.1',srv.server_port,timeout=5)
            headers={'Content-Type':'application/json'}
            if auth: headers['Authorization']='Bearer '+token
            headers.update(extra or {})
            raw=None if payload is None else json.dumps(payload)
            c.request(method,path,body=raw,headers=headers)
            response=c.getresponse()
            result=(response.status,json.loads(response.read()))
            c.close()
            return result
        try:
            with contextlib.redirect_stderr(output),contextlib.redirect_stdout(output):
                self.assertEqual(srv.server_address[0],'127.0.0.1')
                self.assertEqual(request('GET','/health',auth=False)[0],401)
                self.assertEqual(request('GET','/health')[0],200)
                self.assertEqual(request('POST','/v1/sql',{'sql':'SELECT 1'})[0],404)
                self.assertEqual(request('POST','/v1/articles',{})[0],400)
                self.assertEqual(request('POST','/v1/articles',{},extra={'Content-Length':str(service.MAX_BODY+1)})[0],413)
                payload=dict(operationId='http-operation-1',records={'article':[self.article()]})
                first=request('POST','/v1/articles',payload)
                self.assertEqual(first[0],200)
                self.assertEqual(request('POST','/v1/articles',payload),first)
                self.assertEqual(request('GET','/v1/articles/article-1')[0],200)
            self.assertNotIn(token,output.getvalue())
        finally:
            srv.shutdown()
            thread.join()
            srv.server_close()
    def test_external_configuration_rejected(self):
        with patch.dict(os.environ,{'AUTOARTICLE_DB_SERVICE_TOKEN':'x'*40,'AUTOARTICLE_DB_SERVICE_URL':'http://0.0.0.0:8766'}):
            with self.assertRaises(ValueError): service.configuration()

if __name__=='__main__': unittest.main()
